#!/usr/bin/env python3
"""
Teams Bridge — Bidirectional Microsoft Teams communication for Mandali.

This module handles ALL Teams communication via Bot Framework SDK.
It is imported by mandali.py but has zero dependency on mandali internals.

Architecture:
- aiohttp web server on port 3978 receives Bot Framework messages
- Dev Tunnels (devtunnel CLI) provides persistent public URL
- ConversationReference stored in ~/.copilot/mandali-teams-convref.json
- Daemon thread runs the server; queue.Queue delivers replies to main thread
"""

import asyncio
import json
import logging
import os
import queue
import re
import sys
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity, ActivityTypes, ConversationReference

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Security: Maximum message length from Teams (prevents abuse)
MAX_TEAMS_MESSAGE_LENGTH = 4000

# Config file paths
TEAMS_CONFIG_PATH = Path.home() / ".copilot" / "mandali-teams.json"
CONVREF_PATH = Path.home() / ".copilot" / "mandali-teams-convref.json"


def check_file_permissions(path: Path) -> None:
    """
    Check that a sensitive file has secure permissions (Unix only).
    Raises PermissionError if group/other can access the file.
    
    Security: This is fail-secure — we refuse to load insecure credentials.
    """
    if sys.platform == "win32":
        # Windows: NTFS permissions in user profile are generally adequate for v1
        return
    
    if not path.exists():
        return
    
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:  # group or other has any access
        raise PermissionError(
            f"Credential file {path} has insecure permissions ({oct(mode)}). "
            f"Run: chmod 600 {path}"
        )


def load_teams_config() -> Optional[dict]:
    """
    Load Teams credentials from ~/.copilot/mandali-teams.json.
    
    Security: Checks file permissions before loading. Never logs app_password.
    
    Returns:
        dict with app_id and app_password, or None if config doesn't exist.
    
    Raises:
        PermissionError: If file has insecure permissions.
        json.JSONDecodeError: If file contains invalid JSON.
    """
    if not TEAMS_CONFIG_PATH.exists():
        return None
    
    check_file_permissions(TEAMS_CONFIG_PATH)
    
    # Security: Never log the contents of this file
    config = json.loads(TEAMS_CONFIG_PATH.read_text(encoding="utf-8"))
    
    # Validate required fields
    if "app_id" not in config:
        raise ValueError(f"Teams config {TEAMS_CONFIG_PATH} must contain 'app_id'")
    if "app_password" not in config and "cert_thumbprint" not in config:
        raise ValueError(
            f"Teams config {TEAMS_CONFIG_PATH} must contain 'app_password' or 'cert_thumbprint'"
        )
    
    return config


def sanitize_teams_message(text: str) -> str:
    """
    Sanitize a message received from Teams before injection into conversation.
    
    Security:
    - Removes control characters (except tab, newline, carriage return)
    - Truncates to MAX_TEAMS_MESSAGE_LENGTH to prevent abuse
    """
    # Remove control chars except \t (0x09), \n (0x0a), \r (0x0d)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    
    # Length limit
    if len(text) > MAX_TEAMS_MESSAGE_LENGTH:
        text = text[:MAX_TEAMS_MESSAGE_LENGTH] + "... [truncated]"
    
    return text.strip()


def _find_devtunnel_cli() -> Optional[str]:
    """Find the devtunnel CLI binary."""
    path = shutil.which("devtunnel")
    if path:
        return path
    # Check common Windows WinGet location
    winget_path = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Links" / "devtunnel.exe"
    if winget_path.exists():
        return str(winget_path)
    return None


def start_devtunnel(port: int, tunnel_id: Optional[str] = None) -> tuple[subprocess.Popen, str]:
    """
    Start a dev tunnel and return (process, public_url).
    
    If tunnel_id is provided, hosts that existing tunnel (persistent URL).
    Otherwise creates a temporary anonymous tunnel.
    
    Returns:
        Tuple of (subprocess.Popen, public_url_string)
    """
    cli = _find_devtunnel_cli()
    if not cli:
        raise RuntimeError(
            "devtunnel CLI not found. Install with: winget install Microsoft.devtunnel\n"
            "Then login: devtunnel user login"
        )
    
    if tunnel_id:
        # Host existing persistent tunnel
        cmd = [cli, "host", tunnel_id, "--allow-anonymous"]
    else:
        # Create and host a temporary tunnel
        cmd = [cli, "host", "-p", str(port), "--allow-anonymous"]
    
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    
    # Read output until we get the public URL
    public_url = None
    import time
    deadline = time.time() + 30  # 30 second timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"devtunnel exited with code {proc.returncode}")
            continue
        logger.info(f"devtunnel: {line.rstrip()}")
        # Dev tunnels outputs a line like: "Connect via browser: https://XXXX.devtunnels.ms:3978"
        # or "Your tunnel is ready: https://XXXX.devtunnels.ms"
        # or "Hosting port: 3978 at https://XXXX-3978.REGION.devtunnels.ms"
        if "devtunnels.ms" in line:
            import re as _re
            match = _re.search(r'(https://[^\s]+devtunnels\.ms[^\s]*)', line)
            if match:
                public_url = match.group(1).rstrip('/')
                break
    
    if not public_url:
        proc.terminate()
        raise RuntimeError("Failed to get dev tunnel URL within 30 seconds")
    
    return proc, public_url


def stop_devtunnel(proc: subprocess.Popen) -> None:
    """Stop a running dev tunnel process."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def load_conversation_reference()-> Optional[ConversationReference]:
    """Load stored ConversationReference from disk."""
    if not CONVREF_PATH.exists():
        return None
    
    check_file_permissions(CONVREF_PATH)
    
    data = json.loads(CONVREF_PATH.read_text(encoding="utf-8"))
    return ConversationReference().deserialize(data)


def save_conversation_reference(conv_ref: ConversationReference) -> None:
    """
    Save ConversationReference to disk for proactive messaging.
    
    Security: Sets restrictive permissions on Unix.
    """
    # Ensure directory exists
    CONVREF_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Serialize and save
    data = conv_ref.serialize()
    CONVREF_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    
    # Set restrictive permissions on Unix
    if sys.platform != "win32":
        os.chmod(CONVREF_PATH, 0o600)
    
    logger.info(f"Saved ConversationReference to {CONVREF_PATH}")


class TeamsBridge:
    """
    Bidirectional Teams communication via Bot Framework.
    
    Usage:
        bridge = TeamsBridge(app_id, app_password)
        bridge.set_reply_callback(my_callback)
        await bridge.start()
        thread_id = await bridge.create_thread("Hello!")
        await bridge.send_to_thread(thread_id, "Update message")
        await bridge.stop()
    """
    
    def __init__(self, app_id: str, app_password: str = None,
                 cert_thumbprint: str = None, cert_key_path: str = None,
                 tenant_id: str = None):
        """
        Initialize with Azure Bot credentials.
        
        Supports two auth modes:
        1. Client secret: provide app_password
        2. Certificate: provide cert_thumbprint + cert_key_path
        
        Security: app_password/cert keys are never logged.
        
        Args:
            app_id: Azure Bot App ID (GUID)
            app_password: Azure Bot App Password (client secret)
            cert_thumbprint: Certificate SHA1 thumbprint (hex)
            cert_key_path: Path to PEM file with private key
            tenant_id: Azure AD tenant ID (required for cert auth)
        """
        self._app_id = app_id
        
        # Build adapter with appropriate credentials
        if cert_thumbprint and cert_key_path:
            # Certificate-based authentication
            from botframework.connector.auth import CertificateAppCredentials
            cert_key = Path(cert_key_path).read_text(encoding="utf-8")
            app_credentials = CertificateAppCredentials(
                app_id=app_id,
                certificate_thumbprint=cert_thumbprint,
                certificate_private_key=cert_key,
                channel_auth_tenant=tenant_id,
            )
            settings = BotFrameworkAdapterSettings(app_id, app_credentials=app_credentials)
        else:
            # Client secret authentication
            settings = BotFrameworkAdapterSettings(app_id, app_password)
        self._adapter = BotFrameworkAdapter(settings)
        
        # Server state
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._server_thread: Optional[threading.Thread] = None
        self._server_loop: Optional[asyncio.AbstractEventLoop] = None
        self._devtunnel_proc: Optional[subprocess.Popen] = None
        self._public_url: Optional[str] = None
        self._is_running = False
        
        # Conversation state
        self._conv_ref: Optional[ConversationReference] = None
        self._thread_conv_refs: dict[str, ConversationReference] = {}
        
        # Reply handling
        self._reply_callback: Optional[Callable[[str, str], None]] = None
        self._reply_queue: queue.Queue = queue.Queue()
    
    @property
    def is_running(self) -> bool:
        """Whether the bridge is active."""
        return self._is_running
    
    @property
    def public_url(self) -> str:
        """The dev tunnel public URL (for diagnostics)."""
        return self._public_url or ""
    
    def set_reply_callback(self, callback: Callable[[str, str], None]) -> None:
        """
        Register callback for when a Teams reply is received.
        
        The callback is invoked from the server thread. If you need it on
        the main thread, use get_pending_reply() to poll the queue instead.
        
        Args:
            callback: Function(thread_id: str, message_text: str)
        """
        self._reply_callback = callback
    
    def get_pending_reply(self) -> Optional[tuple[str, str]]:
        """
        Non-blocking check for a pending Teams reply.
        
        Returns:
            Tuple of (thread_id, message_text) or None if no reply pending.
        """
        try:
            return self._reply_queue.get_nowait()
        except queue.Empty:
            return None
    
    async def start(self, port: int = 3978) -> None:
        """
        Start the aiohttp web server + dev tunnel.
        
        This launches:
        1. Dev tunnel via devtunnel CLI → public URL
        2. aiohttp server on localhost:port
        3. Server runs in a daemon background thread
        
        Args:
            port: Local port for the webhook server (default 3978)
        """
        if self._is_running:
            logger.warning("TeamsBridge already running")
            return
        
        # Load any existing ConversationReference
        self._conv_ref = load_conversation_reference()
        if self._conv_ref:
            logger.info("Loaded existing ConversationReference from disk")
        
        # Start dev tunnel
        config = load_teams_config() or {}
        tunnel_id = config.get("tunnel_id")
        logger.info(f"Starting dev tunnel on port {port}..." + (f" (tunnel: {tunnel_id})" if tunnel_id else ""))
        self._devtunnel_proc, self._public_url = start_devtunnel(port, tunnel_id)
        logger.info(f"Dev tunnel active: {self._public_url}")
        
        print(f"\n{'='*60}")
        print("TEAMS BRIDGE STARTED")
        print(f"{'='*60}")
        print(f"Public URL: {self._public_url}")
        print(f"Bot Endpoint: {self._public_url}/api/messages")
        print(f"{'='*60}\n")
        
        # Create aiohttp app
        self._app = web.Application()
        self._app.router.add_post("/api/messages", self._handle_messages)
        
        # Start server in background thread
        self._server_thread = threading.Thread(
            target=self._run_server,
            args=(port,),
            daemon=True
        )
        self._server_thread.start()
        
        # Wait for server to be ready
        await asyncio.sleep(1)
        self._is_running = True
        logger.info(f"TeamsBridge server running on port {port}")
    
    def _run_server(self, port: int) -> None:
        """Run the aiohttp server in a background thread."""
        self._server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._server_loop)
        
        async def start_server():
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, "0.0.0.0", port)
            await self._site.start()
            
            # Keep running until stopped
            while self._is_running and not hasattr(self, '_stop_requested'):
                await asyncio.sleep(0.1)
        
        try:
            self._server_loop.run_until_complete(start_server())
        except Exception as e:
            logger.error(f"Server error: {e}")
        finally:
            self._server_loop.close()
    
    async def stop(self) -> None:
        """Shut down server + dev tunnel cleanly."""
        if not self._is_running:
            return
        
        logger.info("Stopping TeamsBridge...")
        self._is_running = False
        self._stop_requested = True
        
        # Stop dev tunnel
        if self._devtunnel_proc:
            stop_devtunnel(self._devtunnel_proc)
            self._devtunnel_proc = None
            logger.info("Dev tunnel closed")
        
        # Stop aiohttp
        if self._runner:
            await self._runner.cleanup()
        
        logger.info("TeamsBridge stopped")
    
    async def _handle_messages(self, request: web.Request) -> web.Response:
        """Handle incoming Bot Framework messages."""
        if "application/json" not in request.content_type:
            return web.Response(status=415)
        
        body = await request.json()
        activity = Activity().deserialize(body)
        
        logger.info(f"Received activity: type={activity.type}, from={activity.from_property.name if activity.from_property else 'unknown'}")
        
        # Process the activity
        auth_header = request.headers.get("Authorization", "")
        
        async def turn_callback(turn_context: TurnContext):
            await self._on_message(turn_context)
        
        try:
            await self._adapter.process_activity(activity, auth_header, turn_callback)
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"Error processing activity: {e}")
            return web.Response(status=500, text=str(e))
    
    async def _on_message(self, turn_context: TurnContext) -> None:
        """Handle a message from Teams."""
        activity = turn_context.activity
        
        # Store ConversationReference for proactive messaging
        conv_ref = TurnContext.get_conversation_reference(activity)
        
        # On first message/install, save the base reference
        if activity.type == ActivityTypes.conversation_update:
            # Bot was added to channel
            if activity.members_added:
                for member in activity.members_added:
                    if member.id != activity.recipient.id:
                        # A user was added, not the bot
                        continue
                # Bot was added — save reference
                self._conv_ref = conv_ref
                save_conversation_reference(conv_ref)
                logger.info("Bot installed in channel — ConversationReference saved")
            return
        
        if activity.type != ActivityTypes.message:
            return
        
        # It's a message — extract text and notify callback
        text = activity.text or ""
        text = sanitize_teams_message(text)
        
        if not text:
            return
        
        # Get thread/conversation ID
        thread_id = activity.conversation.id if activity.conversation else "unknown"
        
        # Store reference for this thread
        self._thread_conv_refs[thread_id] = conv_ref
        
        logger.info(f"Teams message received in thread {thread_id[:20]}...: {text[:50]}...")
        
        # Put in queue for main thread polling
        self._reply_queue.put((thread_id, text))
        
        # Also call direct callback if set
        if self._reply_callback:
            try:
                self._reply_callback(thread_id, text)
            except Exception as e:
                logger.error(f"Reply callback error: {e}")
        
        # Send acknowledgment
        await turn_context.send_activity("✓ Received — relaying to Mandali agents.")
    
    async def create_thread(self, message: str) -> str:
        """
        Create a new thread in the Teams channel by sending the first message.
        
        Args:
            message: The initial message to post
            
        Returns:
            Thread ID (conversation ID) for subsequent messages
            
        Raises:
            RuntimeError: If no ConversationReference is available (bot not installed)
        """
        if not self._conv_ref:
            raise RuntimeError(
                "No ConversationReference available. "
                "The bot must be installed in a Teams channel first. "
                "Add the bot to a channel and send it a message to initialize."
            )
        
        thread_id = None
        
        async def send_callback(turn_context: TurnContext):
            nonlocal thread_id
            response = await turn_context.send_activity(message)
            # The response contains the new conversation/thread ID
            thread_id = turn_context.activity.conversation.id
            # Store reference for this thread
            self._thread_conv_refs[thread_id] = TurnContext.get_conversation_reference(
                turn_context.activity
            )
            logger.info(f"Created thread: {thread_id[:30]}...")
        
        await self._adapter.continue_conversation(
            self._conv_ref,
            send_callback,
            self._app_id
        )
        
        return thread_id or self._conv_ref.conversation.id
    
    async def send_to_thread(self, thread_id: str, message: str) -> None:
        """
        Send a message as a reply in an existing thread.
        
        Args:
            thread_id: The conversation/thread ID to reply to
            message: The message text to send
        """
        # Get the ConversationReference for this thread
        conv_ref = self._thread_conv_refs.get(thread_id)
        
        if not conv_ref:
            # Fall back to base reference
            conv_ref = self._conv_ref
            if not conv_ref:
                raise RuntimeError("No ConversationReference available for thread")
        
        async def send_callback(turn_context: TurnContext):
            await turn_context.send_activity(message)
            logger.info(f"Sent to thread {thread_id[:20]}...: {message[:50]}...")
        
        await self._adapter.continue_conversation(
            conv_ref,
            send_callback,
            self._app_id
        )


# =============================================================================
# Cloud Relay Bridge (new architecture — WebSocket client to Azure relay)
# =============================================================================

class TeamsRelayBridge:
    """
    Teams integration via the cloud relay service.

    Connects to the Azure-hosted relay via WebSocket. The relay handles all
    Bot Framework auth (MSI). This client just sends/receives JSON messages.

    Same public interface as TeamsBridge so mandali.py works unchanged.
    """

    def __init__(self, relay_url: str, api_key: str):
        self._relay_url = relay_url
        self._api_key = api_key
        self._ws = None
        self._connection_id = None
        self._is_running = False
        self._reply_callback: Optional[Callable[[str, str], None]] = None
        self._reply_queue: queue.Queue = queue.Queue()
        self._listen_task: Optional[asyncio.Task] = None
        self._registered_threads: set = set()

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def public_url(self) -> str:
        return self._relay_url

    def set_reply_callback(self, callback: Callable[[str, str], None]) -> None:
        self._reply_callback = callback

    def get_pending_reply(self) -> Optional[tuple]:
        try:
            return self._reply_queue.get_nowait()
        except queue.Empty:
            return None

    async def start(self) -> None:
        """Connect to the relay WebSocket and authenticate."""
        if self._is_running:
            return

        try:
            import websockets
        except ImportError:
            raise RuntimeError(
                "websockets package required: pip install websockets"
            )

        headers = {"Authorization": f"Bearer {self._api_key}"}
        self._ws = await websockets.connect(
            self._relay_url,
            additional_headers=headers,
            ping_interval=None,  # Disable library-level keepalive; relay sends JSON pings
        )
        self._is_running = True
        logger.info(f"Connected to relay: {self._relay_url}")

        # Start background listener for incoming messages
        self._listen_task = asyncio.create_task(self._listen_loop())

        # Register for all threads so we receive every @mention
        await self._ws.send(json.dumps({"type": "register_all"}))

    async def stop(self) -> None:
        """Disconnect from the relay."""
        self._is_running = False
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        logger.info("Disconnected from relay")

    async def _listen_loop(self) -> None:
        """Background task: receive messages from relay with auto-reconnect."""
        while self._is_running:
            try:
                async for raw in self._ws:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")
                    data = msg.get("data", {})

                    if msg_type == "ping":
                        await self._ws.send(json.dumps({"type": "pong"}))

                    elif msg_type == "teams_message":
                        text = data.get("text", "")
                        thread_id = data.get("thread_id", "unknown")
                        # Strip @mention tags
                        text = re.sub(r'<at>[^<]*</at>\s*', '', text).strip()
                        if not text:
                            continue
                        self._reply_queue.put((thread_id, text))
                        if self._reply_callback:
                            try:
                                self._reply_callback(thread_id, text)
                            except Exception as e:
                                logger.error(f"Reply callback error: {e}")

                    elif msg_type == "auth_success":
                        self._connection_id = data.get("connection_id")
                        logger.info(f"Authenticated: {self._connection_id}")

                    elif msg_type == "thread_registered":
                        tid = data.get("thread_id", "")
                        self._registered_threads.add(tid)
                        logger.info(f"Thread registered: {tid[:30]}...")

                    elif msg_type == "registered_all":
                        count = data.get("thread_count", 0)
                        tids = data.get("thread_ids", [])
                        self._registered_threads.update(tids)
                        logger.info(f"Registered for all threads ({count} existing)")

                    elif msg_type == "message_sent":
                        success = data.get("success", False)
                        if not success:
                            logger.warning(f"Relay could not deliver message to Teams")

                    elif msg_type == "error":
                        logger.error(f"Relay error: {data.get('message', 'unknown')}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._is_running:
                    return
                logger.warning(f"WebSocket disconnected: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
                try:
                    import websockets
                    headers = {"Authorization": f"Bearer {self._api_key}"}
                    self._ws = await websockets.connect(
                        self._relay_url,
                        additional_headers=headers,
                        ping_interval=None,
                    )
                    await self._ws.send(json.dumps({"type": "register_all"}))
                    logger.info("Reconnected to relay")
                except Exception as re_err:
                    logger.error(f"Reconnect failed: {re_err}")

    async def register_thread(self, thread_id: str) -> None:
        """Register to receive messages for a specific Teams thread."""
        if self._ws:
            await self._ws.send(json.dumps({
                "type": "register_thread",
                "data": {"thread_id": thread_id}
            }))

    async def send_to_thread(self, thread_id: str, message: str) -> None:
        """Send a message to a Teams thread via the relay."""
        if not self._ws:
            raise RuntimeError("Not connected to relay")
        await self._ws.send(json.dumps({
            "type": "send_message",
            "data": {"thread_id": thread_id, "text": message}
        }))

    async def create_thread(self, message: str) -> str:
        """
        Send to the default channel thread via the relay.

        The relay uses whichever conversation reference it has stored.
        Returns a placeholder thread_id — the relay will use the first
        known thread. For full thread creation, @mention the bot first.
        """
        if not self._ws:
            raise RuntimeError("Not connected to relay")
        # Ask relay to send the message; it'll use its stored conv ref
        await self._ws.send(json.dumps({
            "type": "send_message",
            "data": {"thread_id": "__default__", "text": message}
        }))
        # Return a sentinel — the real thread_id comes when the relay
        # delivers the first incoming message for this conversation
        return "__default__"


def load_relay_config() -> Optional[dict]:
    """
    Load relay config from ~/.copilot/mandali-teams.json.

    Returns dict with relay_url and api_key, or None if not configured.
    """
    if not TEAMS_CONFIG_PATH.exists():
        return None

    data = json.loads(TEAMS_CONFIG_PATH.read_text(encoding="utf-8"))

    if "relay_url" in data and "api_key" in data:
        return data
    return None


# =============================================================================
# Smoke Test
# =============================================================================

if __name__ == "__main__":
    """
    Smoke test: start bridge, wait for messages, allow sending test messages.
    
    Usage:
        python teams_bridge.py
        
    Prerequisites:
        1. Create ~/.copilot/mandali-teams.json with app_id and app_password
        2. Register an Azure Bot and configure the messaging endpoint
        3. Install the bot in a Teams channel
    """
    import sys
    
    print("Teams Bridge Smoke Test")
    print("=" * 40)
    
    # Load config
    config = load_teams_config()
    if not config:
        print(f"\nNo config found at {TEAMS_CONFIG_PATH}")
        print("\nCreate the file with:")
        print(json.dumps({
            "app_id": "YOUR-APP-ID-GUID",
            "app_password": "YOUR-APP-PASSWORD"
        }, indent=2))
        sys.exit(1)
    
    print(f"Config loaded from {TEAMS_CONFIG_PATH}")
    print(f"App ID: {config['app_id']}")
    print("App Password: ******* (hidden)")
    
    # Create bridge
    bridge = TeamsBridge(config["app_id"], config["app_password"])
    
    # Set up reply callback
    def on_reply(thread_id: str, text: str):
        print(f"\n[TEAMS REPLY] Thread: {thread_id[:30]}...")
        print(f"[TEAMS REPLY] Text: {text}")
        print()
    
    bridge.set_reply_callback(on_reply)
    
    async def main():
        # Start bridge
        await bridge.start()
        
        print("\nBridge is running!")
        print("- Send a message to the bot in Teams to test receiving")
        print("- Type 'send' to send a test message (requires bot to be installed)")
        print("- Type 'quit' to exit")
        print()
        
        # Interactive loop
        while True:
            try:
                cmd = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input("> ").strip().lower()
                )
                
                if cmd == "quit":
                    break
                elif cmd == "send":
                    try:
                        thread_id = await bridge.create_thread(
                            f"🧪 Test message from Mandali at {datetime.now().strftime('%H:%M:%S')}"
                        )
                        print(f"Sent! Thread ID: {thread_id[:30]}...")
                    except RuntimeError as e:
                        print(f"Cannot send: {e}")
                elif cmd:
                    print("Commands: 'send', 'quit'")
                    
            except (KeyboardInterrupt, EOFError):
                break
        
        # Cleanup
        await bridge.stop()
        print("\nBridge stopped.")
    
    asyncio.run(main())
