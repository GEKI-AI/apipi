"""ApiPi is a drop-in OpenAI Agents API.

Point official clients at this gateway and bring your own model URL.
"""

__version__ = "0.2.0"

from apipi.agents import AgentWrite
from apipi.auth import Authenticate, AuthIdentity, AuthReject
from apipi.config import Settings, extend_settings
from apipi.gateway import Gateway, create_app
from apipi.runtime import EventHub
from apipi.sessions import SessionService
from apipi.store.engine import Store

__all__ = [
    "AgentWrite",
    "AuthIdentity",
    "AuthReject",
    "Authenticate",
    "EventHub",
    "Gateway",
    "SessionService",
    "Settings",
    "Store",
    "__version__",
    "create_app",
    "extend_settings",
]
