from typing import Any

SESSION_KIND_KEY = "apipi.session_kind"
CHAT = "chat"
MICROVM = "microvm"
REJECT = "reject"


def placement_for(
    *,
    environment: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
    env_none: str,
) -> str | None:
    if (metadata or {}).get(SESSION_KIND_KEY) == CHAT:
        return CHAT
    env_type = (environment or {}).get("type") or "openai_hosted"
    if env_type == "none":
        if env_none == REJECT:
            return None
        return env_none
    return MICROVM


def worker_accepts(process_mode: str, required: str) -> bool:
    if process_mode == required:
        return True
    return required == CHAT and process_mode == "none"
