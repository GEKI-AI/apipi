from collections.abc import Collection
from typing import Any

NONE = "none"
MICROVM = "microvm"


def placement_for(*, environment: dict[str, Any] | None) -> str:
    env_type = (environment or {}).get("type") or "openai_hosted"
    if env_type == "none":
        return NONE
    return MICROVM


def worker_accepts(accepts: Collection[str] | None, required: str) -> bool:
    if accepts is None:
        return False
    return required in set(accepts)
