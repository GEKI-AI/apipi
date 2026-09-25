"""ApiPi is a drop-in OpenAI Agents API.

Point official clients at this gateway and bring your own model URL.
"""

__version__ = "0.5.1"

from apipi.config import Settings, extend_settings
from apipi.gateway import Gateway, create_app
from apipi.gateway.auth import (
    Authenticate,
    AuthIdentity,
    AuthReject,
    AuthRequest,
    tenant_from_key,
)
from apipi.services.agents import AgentWrite
from apipi.services.runtime import EventHub
from apipi.services.sessions import SessionService
from apipi.store.engine import Store

__all__ = [
    "AgentWrite",
    "AuthIdentity",
    "AuthReject",
    "AuthRequest",
    "Authenticate",
    "EventHub",
    "Gateway",
    "SessionService",
    "Settings",
    "Store",
    "__version__",
    "create_app",
    "extend_settings",
    "tenant_from_key",
]
