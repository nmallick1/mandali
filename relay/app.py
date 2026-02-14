"""
Mandali Teams Relay — FastAPI Application

Main entry point for the relay service. Wires together:
- Bot Framework webhook at /api/messages
- WebSocket endpoint at /ws for mandali instances
- Health check at /health

Authentication:
- Bot Framework: MSI-based (no app password)
- WebSocket: API key via Authorization header
"""
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response, HTTPException

from bot_handler import BotHandler
from ws_manager import WSManager
from config import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global instances
ws_manager = WSManager()
bot_handler: BotHandler = None
ping_task: asyncio.Task = None


async def ping_loop():
    """Background task to send periodic pings to all connected clients."""
    while True:
        await asyncio.sleep(config.PING_INTERVAL)
        if ws_manager.authenticated:
            logger.debug(f"Sending ping to {len(ws_manager.authenticated)} clients")
            await ws_manager.send_ping_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global bot_handler, ping_task
    
    # Startup
    logger.info("Starting Mandali Teams Relay...")
    logger.info(f"App ID: {config.MICROSOFT_APP_ID or '(not configured)'}")
    logger.info(f"Tenant: {config.MICROSOFT_APP_TENANT_ID or '(not configured)'}")
    logger.info(f"API Key configured: {'Yes' if config.WS_API_KEY else 'No'}")
    
    bot_handler = BotHandler(
        app_id=config.MICROSOFT_APP_ID,
        app_type=config.MICROSOFT_APP_TYPE,
        tenant_id=config.MICROSOFT_APP_TENANT_ID,
        ws_manager=ws_manager,
    )
    
    # Start ping loop
    ping_task = asyncio.create_task(ping_loop())
    
    logger.info("Relay started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down relay...")
    if ping_task:
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Mandali Teams Relay", lifespan=lifespan)


@app.post("/api/messages")
async def bot_messages(request: Request):
    """
    Bot Framework webhook — receives all messages from Teams.
    
    Bot Framework validates the JWT token in the Authorization header
    automatically via the adapter.
    """
    if bot_handler is None:
        raise HTTPException(status_code=503, detail="Bot handler not initialized")
    
    try:
        body = await request.json()
    except Exception as e:
        logger.error(f"Invalid JSON in request: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    auth_header = request.headers.get("Authorization", "")
    
    try:
        response = await bot_handler.process_activity(body, auth_header)
        if response:
            return Response(
                status_code=response.status,
                content=response.body if hasattr(response, 'body') else None
            )
        return Response(status_code=200)
    except Exception as e:
        logger.error(f"Error processing bot activity: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket endpoint for mandali instances.
    
    Authentication: Client must send {"type": "auth", "data": {"api_key": "xxx"}}
    as the first message. Alternatively, can use Authorization: Bearer xxx header.
    """
    connection_id = str(uuid.uuid4())
    await ws_manager.connect(websocket, connection_id)
    
    # Check for header-based auth (simpler approach)
    auth_header = websocket.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        provided_key = auth_header[7:]
        if config.WS_API_KEY:
            # API key is configured — validate it
            if provided_key == config.WS_API_KEY:
                ws_manager.authenticate(connection_id)
            else:
                logger.warning(f"Invalid API key in header from {connection_id}")
                await websocket.close(code=4001, reason="Invalid API key")
                ws_manager.disconnect(connection_id)
                return
        else:
            # No API key configured (dev mode) — accept any header auth
            ws_manager.authenticate(connection_id)
    elif not config.WS_API_KEY:
        # No API key configured AND no header — auto-authenticate (dev mode)
        ws_manager.authenticate(connection_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            await handle_ws_message(connection_id, data, websocket)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {connection_id}")
    except Exception as e:
        logger.error(f"WebSocket error for {connection_id}: {e}")
    finally:
        ws_manager.disconnect(connection_id)


async def handle_ws_message(connection_id: str, message: dict, websocket: WebSocket):
    """Handle a message from a mandali instance."""
    msg_type = message.get("type")
    data = message.get("data", {})
    
    # Handle auth message (if not already authenticated via header)
    if msg_type == "auth":
        if ws_manager.is_authenticated(connection_id):
            return  # Already authenticated
        
        provided_key = data.get("api_key", "")
        if config.WS_API_KEY and provided_key != config.WS_API_KEY:
            logger.warning(f"Invalid API key from {connection_id}")
            await websocket.close(code=4001, reason="Invalid API key")
            return
        
        ws_manager.authenticate(connection_id)
        await ws_manager.send_to_connection(connection_id, {
            "type": "auth_success",
            "data": {"connection_id": connection_id}
        })
        return
    
    # All other messages require authentication
    if not ws_manager.is_authenticated(connection_id):
        logger.warning(f"Unauthenticated message from {connection_id}: {msg_type}")
        await websocket.close(code=4001, reason="Not authenticated")
        return
    
    if msg_type == "send_message":
        thread_id = data.get("thread_id")
        text = data.get("text")
        
        if not thread_id or not text:
            await ws_manager.send_to_connection(connection_id, {
                "type": "error",
                "data": {"message": "Missing thread_id or text", "code": "INVALID_REQUEST"}
            })
            return
        
        success = await bot_handler.send_proactive(thread_id, text)
        await ws_manager.send_to_connection(connection_id, {
            "type": "message_sent",
            "data": {"thread_id": thread_id, "success": success}
        })
    
    elif msg_type == "register_thread":
        thread_id = data.get("thread_id")
        if thread_id:
            ws_manager.register_thread(thread_id, connection_id)
            await ws_manager.send_to_connection(connection_id, {
                "type": "thread_registered",
                "data": {"thread_id": thread_id}
            })
    
    elif msg_type == "register_all":
        # Register this connection for ALL current and future threads
        ws_manager.set_default_connection(connection_id)
        known = bot_handler.get_thread_ids() if bot_handler else []
        for tid in known:
            ws_manager.register_thread(tid, connection_id)
        await ws_manager.send_to_connection(connection_id, {
            "type": "registered_all",
            "data": {"thread_count": len(known), "thread_ids": known}
        })
    
    elif msg_type == "pong":
        # Keepalive response — no action needed
        pass
    
    else:
        logger.warning(f"Unknown message type from {connection_id}: {msg_type}")


@app.get("/health")
async def health():
    """
    Health check for App Service.
    
    Returns connection statistics for monitoring.
    """
    stats = ws_manager.get_stats()
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        **stats,
        "bot_configured": bool(config.MICROSOFT_APP_ID),
        "known_threads": len(bot_handler.get_thread_ids()) if bot_handler else 0,
    }


@app.get("/")
async def root():
    """Root endpoint — basic info."""
    return {
        "service": "Mandali Teams Relay",
        "version": "1.0.0",
        "endpoints": {
            "bot_webhook": "/api/messages",
            "websocket": "/ws",
            "health": "/health",
        }
    }
