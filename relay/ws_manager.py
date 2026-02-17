"""
WebSocket Connection Manager for Mandali Teams Relay.

Manages connected mandali instances. Each WebSocket connection is identified
by a connection ID and can be associated with Teams conversation threads.
"""
import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WSManager:
    """Manages WebSocket connections from mandali instances."""
    
    def __init__(self):
        self.connections: Dict[str, WebSocket] = {}  # connection_id → WebSocket
        self.thread_to_connection: Dict[str, str] = {}  # thread_id → connection_id
        self.authenticated: set = set()  # Set of authenticated connection_ids
        self.default_connection: Optional[str] = None  # Catches all unregistered threads
        self.last_ping_at: Optional[datetime] = None
    
    async def connect(self, websocket: WebSocket, connection_id: str):
        """Accept and register a new WebSocket connection."""
        await websocket.accept()
        self.connections[connection_id] = websocket
        logger.info(f"Client connected: {connection_id}")
    
    def disconnect(self, connection_id: str):
        """Remove a disconnected client."""
        self.connections.pop(connection_id, None)
        self.authenticated.discard(connection_id)
        if self.default_connection == connection_id:
            self.default_connection = None
        # Clean up thread mappings
        self.thread_to_connection = {
            tid: cid for tid, cid in self.thread_to_connection.items() 
            if cid != connection_id
        }
        logger.info(f"Client disconnected: {connection_id}")
    
    def authenticate(self, connection_id: str):
        """Mark a connection as authenticated."""
        self.authenticated.add(connection_id)
        logger.info(f"Client authenticated: {connection_id}")
    
    def is_authenticated(self, connection_id: str) -> bool:
        """Check if a connection is authenticated."""
        return connection_id in self.authenticated
    
    def register_thread(self, thread_id: str, connection_id: str):
        """Map a Teams thread to a mandali connection."""
        self.thread_to_connection[thread_id] = connection_id
        logger.info(f"Thread {thread_id[:30]}... registered to {connection_id}")
    
    def set_default_connection(self, connection_id: str):
        """Set a connection as the default for all unregistered threads."""
        self.default_connection = connection_id
        logger.info(f"Default connection set to {connection_id}")
    
    async def send_to_connection(self, connection_id: str, message: dict) -> bool:
        """Send a JSON message to a specific mandali instance."""
        ws = self.connections.get(connection_id)
        if ws and connection_id in self.authenticated:
            try:
                await ws.send_json(message)
                return True
            except Exception as e:
                logger.error(f"Failed to send to {connection_id}: {e}")
                self.disconnect(connection_id)
        return False
    
    async def send_to_thread_owner(self, thread_id: str, message: dict) -> bool:
        """Send a message to the mandali instance that owns a thread."""
        connection_id = self.thread_to_connection.get(thread_id)
        if not connection_id and self.default_connection:
            # Fall back to default connection (register_all mode)
            connection_id = self.default_connection
            # Auto-register this thread for future messages
            self.thread_to_connection[thread_id] = connection_id
        if connection_id:
            return await self.send_to_connection(connection_id, message)
        else:
            logger.warning(f"No mandali instance registered for thread {thread_id[:30]}...")
            return False
    
    async def broadcast(self, message: dict):
        """Send a message to ALL authenticated mandali instances."""
        for connection_id in list(self.authenticated):
            await self.send_to_connection(connection_id, message)
    
    async def send_ping_all(self):
        """Send ping to all authenticated connections."""
        self.last_ping_at = datetime.utcnow()
        for connection_id in list(self.authenticated):
            await self.send_to_connection(connection_id, {"type": "ping"})
    
    def get_stats(self) -> dict:
        """Get connection statistics for health endpoint."""
        return {
            "total_connections": len(self.connections),
            "authenticated_connections": len(self.authenticated),
            "registered_threads": len(self.thread_to_connection),
            "last_ping_at": self.last_ping_at.isoformat() if self.last_ping_at else None,
        }
