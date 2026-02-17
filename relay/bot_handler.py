"""
Bot Framework Handler for Mandali Teams Relay.

Handles incoming Bot Framework activities and proactive messaging.
Uses CloudAdapter with ManagedIdentityServiceClientCredentialsFactory
for User-Assigned MSI auth in Azure App Service.
"""
import logging
from typing import Dict

from botbuilder.core import TurnContext
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ActivityTypes, ConversationReference
from botframework.connector.auth import ManagedIdentityServiceClientCredentialsFactory

from utils import sanitize_teams_message

logger = logging.getLogger(__name__)


class _BotConfig:
    """Config object for ConfigurationBotFrameworkAuthentication."""
    def __init__(self, app_id: str, tenant_id: str):
        self.APP_ID = app_id
        self.APP_PASSWORD = ""
        self.APP_TYPE = "UserAssignedMSI"
        self.APP_TENANTID = tenant_id


class BotHandler:
    """Handles Bot Framework messages and proactive messaging."""
    
    def __init__(self, app_id: str, app_type: str, tenant_id: str, ws_manager):
        """
        Initialize CloudAdapter with MSI credentials.
        
        Args:
            app_id: MSI client ID (used as MicrosoftAppId)
            app_type: "UserAssignedMSI" for MSI auth
            tenant_id: Azure AD tenant ID
            ws_manager: WSManager instance for forwarding messages
        """
        self.app_id = app_id
        self.ws_manager = ws_manager
        self.conversation_refs: Dict[str, ConversationReference] = {}
        
        # Always use MSI credentials factory (skips auth when app_id is empty)
        credentials_factory = ManagedIdentityServiceClientCredentialsFactory(
            app_id=app_id or ""
        )
        if app_id:
            logger.info(f"Using MSI credentials for app_id: {app_id}")
        
        config = _BotConfig(app_id or "", tenant_id or "")
        auth = ConfigurationBotFrameworkAuthentication(
            config,
            credentials_factory=credentials_factory,
        )
        self.adapter = CloudAdapter(auth)
        
        # Error handler
        async def on_error(context: TurnContext, error: Exception):
            logger.error(f"Bot error: {error}")
            try:
                await context.send_activity("Sorry, an error occurred.")
            except Exception:
                pass
        
        self.adapter.on_turn_error = on_error
    
    async def process_activity(self, body: dict, auth_header: str):
        """
        Process an incoming Bot Framework activity.
        
        The CloudAdapter handles JWT validation using MSI credentials.
        """
        activity = Activity.deserialize(body)
        
        response = await self.adapter.process_activity(
            auth_header, activity, self._on_turn
        )
        return response
    
    async def _on_turn(self, turn_context: TurnContext):
        """Handle a turn (message or event) from Teams."""
        activity = turn_context.activity
        
        # Store ConversationReference for proactive messaging
        conv_ref = TurnContext.get_conversation_reference(activity)
        thread_id = activity.conversation.id if activity.conversation else "unknown"
        self.conversation_refs[thread_id] = conv_ref
        
        # Handle conversation updates (bot added/removed)
        if activity.type == ActivityTypes.conversation_update:
            if activity.members_added:
                for member in activity.members_added:
                    if member.id == activity.recipient.id:
                        logger.info(f"Bot added to conversation: {thread_id[:30]}...")
            return
        
        # Handle messages
        if activity.type == ActivityTypes.message and activity.text:
            text = sanitize_teams_message(activity.text)
            
            if not text:
                return
            
            sender = activity.from_property.name if activity.from_property else "Unknown"
            timestamp = activity.timestamp.isoformat() if activity.timestamp else ""
            
            logger.info(f"Teams message from {sender} in {thread_id[:30]}...: {text[:50]}...")
            
            # Forward to mandali via WebSocket
            sent = await self.ws_manager.send_to_thread_owner(thread_id, {
                "type": "teams_message",
                "data": {
                    "text": text,
                    "thread_id": thread_id,
                    "sender": sender,
                    "timestamp": timestamp,
                }
            })
            
            if sent:
                try:
                    await turn_context.send_activity("✓ Received — relaying to Mandali agents.")
                except Exception as e:
                    logger.warning(f"Could not send reply: {e}")
            else:
                try:
                    await turn_context.send_activity(
                        "⚠️ No Mandali instance connected for this thread. "
                        "Start mandali with --teams flag first."
                    )
                except Exception as e:
                    logger.warning(f"Could not send reply: {e}")
    
    async def send_proactive(self, thread_id: str, text: str) -> bool:
        """Send a proactive message to a Teams thread."""
        ref = self.conversation_refs.get(thread_id)
        if not ref and thread_id == "__default__" and self.conversation_refs:
            # Use the first available conversation reference
            first_tid = next(iter(self.conversation_refs))
            ref = self.conversation_refs[first_tid]
            thread_id = first_tid
        if not ref:
            logger.warning(f"No conversation reference for thread {thread_id[:30]}...")
            return False
        
        async def callback(turn_context: TurnContext):
            await turn_context.send_activity(text)
        
        try:
            await self.adapter.continue_conversation(
                ref, callback, bot_app_id=self.app_id
            )
            logger.info(f"Sent proactive message to {thread_id[:30]}...")
            return True
        except Exception as e:
            logger.error(f"Failed to send proactive message: {e}")
            return False
    
    def get_thread_ids(self) -> list:
        """Get list of known thread IDs (for diagnostics)."""
        return list(self.conversation_refs.keys())
