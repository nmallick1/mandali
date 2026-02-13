"""
Utility functions for the relay service.

Includes message sanitization copied from teams_bridge.py.
"""
import re

# Security: Maximum message length from Teams (prevents abuse)
MAX_TEAMS_MESSAGE_LENGTH = 4000


def sanitize_teams_message(text: str) -> str:
    """
    Sanitize a message received from Teams before forwarding to mandali.
    
    Security:
    - Removes control characters (except tab, newline, carriage return)
    - Truncates to MAX_TEAMS_MESSAGE_LENGTH to prevent abuse
    
    This function is the trust boundary — messages are sanitized BEFORE
    being sent to mandali instances.
    """
    # Remove control chars except \t (0x09), \n (0x0a), \r (0x0d)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    
    # Length limit
    if len(text) > MAX_TEAMS_MESSAGE_LENGTH:
        text = text[:MAX_TEAMS_MESSAGE_LENGTH] + "... [truncated]"
    
    return text.strip()
