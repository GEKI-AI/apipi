import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from apipi.gateway.handle import Gateway, GatewayRouters, create_app

__all__ = ["Gateway", "GatewayRouters", "create_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        value = getattr(importlib.import_module("apipi.gateway.handle"), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module 'apipi.gateway' has no attribute {name!r}")
