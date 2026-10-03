"""ApiPi is a drop-in OpenAI Agents API.

Point official clients at this gateway and bring your own model URL.

The public names load on first use, so a worker process that only
imports `apipi.worker` never loads the API, the store, or FastAPI.
"""

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.13.0"

if TYPE_CHECKING:
    from apipi.common.event_bus import EventBus, EventHub, InMemoryEventBus
    from apipi.config import Settings, extend_settings
    from apipi.gateway import Gateway, create_app
    from apipi.gateway.auth import (
        Authenticate,
        AuthFilter,
        AuthIdentity,
        Authorize,
        AuthReject,
        AuthRequest,
        tenant_from_key,
    )
    from apipi.services.agents import AgentWrite
    from apipi.services.event_bus import PostgresEventBus
    from apipi.services.sessions import SessionService
    from apipi.store.engine import Store

_EXPORTS = {
    "AgentWrite": "apipi.services.agents",
    "AuthFilter": "apipi.gateway.auth",
    "AuthIdentity": "apipi.gateway.auth",
    "AuthReject": "apipi.gateway.auth",
    "AuthRequest": "apipi.gateway.auth",
    "Authenticate": "apipi.gateway.auth",
    "Authorize": "apipi.gateway.auth",
    "EventBus": "apipi.common.event_bus",
    "EventHub": "apipi.common.event_bus",
    "Gateway": "apipi.gateway",
    "InMemoryEventBus": "apipi.common.event_bus",
    "PostgresEventBus": "apipi.services.event_bus",
    "SessionService": "apipi.services.sessions",
    "Settings": "apipi.config",
    "Store": "apipi.store.engine",
    "create_app": "apipi.gateway",
    "extend_settings": "apipi.config",
    "tenant_from_key": "apipi.gateway.auth",
}

__all__ = [
    "AgentWrite",
    "AuthFilter",
    "AuthIdentity",
    "AuthReject",
    "AuthRequest",
    "Authenticate",
    "Authorize",
    "EventBus",
    "EventHub",
    "Gateway",
    "InMemoryEventBus",
    "PostgresEventBus",
    "SessionService",
    "Settings",
    "Store",
    "__version__",
    "create_app",
    "extend_settings",
    "tenant_from_key",
]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'apipi' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
