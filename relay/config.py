"""
Relay service configuration from environment variables.

Environment variables (set by App Service):
- MICROSOFT_APP_ID: Bot Framework App ID (MSI client ID)
- MICROSOFT_APP_TENANT_ID: Azure AD tenant ID
- WS_API_KEY: API key for WebSocket authentication
- PORT: Server port (default 8000)
"""
import os


class Config:
    """Relay service configuration from environment variables."""
    
    # Bot Framework (MSI auth — no password!)
    MICROSOFT_APP_ID: str = os.environ.get("MICROSOFT_APP_ID", "")
    MICROSOFT_APP_TYPE: str = "UserAssignedMSI"
    MICROSOFT_APP_TENANT_ID: str = os.environ.get("MICROSOFT_APP_TENANT_ID", "")
    
    # WebSocket auth
    WS_API_KEY: str = os.environ.get("WS_API_KEY", "")
    
    # Server
    PORT: int = int(os.environ.get("PORT", "8000"))
    
    # Keepalive interval (seconds)
    PING_INTERVAL: int = int(os.environ.get("PING_INTERVAL", "60"))


config = Config()
