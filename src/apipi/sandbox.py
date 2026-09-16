from typing import Any

from apipi.config import Settings
from apipi.errors import ApiError

SANDBOX_SIZES = frozenset({"S", "M", "L"})
SANDBOX_SIZE_KEY = "apipi.sandbox_size"
SANDBOX_SIZE_HELP = "sandbox_size must be S, M, or L"


def parse_sandbox_size(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_SIZES:
        raise ApiError(
            "invalid_request",
            SANDBOX_SIZE_HELP,
            code="invalid_request",
        )
    return value


def sandbox_size_of(environment: dict[str, Any] | None) -> str:
    if not environment:
        return "S"
    raw = environment.get("sandbox_size")
    if raw is None:
        return "S"
    parsed = parse_sandbox_size(raw)
    return parsed if parsed is not None else "S"


def image_for_size(size: str) -> str:
    if size == "L":
        return "browser"
    return "default"


def size_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    if SANDBOX_SIZE_KEY not in metadata:
        return None
    return parse_sandbox_size(metadata.get(SANDBOX_SIZE_KEY))


def resolve_sandbox_size(
    *,
    environment_size: str | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    default: str,
) -> str:
    if environment_size is not None:
        parsed = parse_sandbox_size(environment_size)
        if parsed is not None:
            return parsed
    session_size = size_from_metadata(session_metadata)
    if session_size is not None:
        return session_size
    agent_size = size_from_metadata(agent_metadata)
    if agent_size is not None:
        return agent_size
    parsed = parse_sandbox_size(default)
    return parsed if parsed is not None else "S"


def mem_mib_for_size(settings: Settings, size: str | None) -> int:
    return settings.sandbox_mem_mib(size if size is not None else "S")


def require_size_rootfs(settings: Settings, size: str) -> None:
    if size != "L" or settings.api_only or settings.run_mode != "microvm":
        return
    from apipi.config import ConfigError
    from apipi.pi.microvm import microvm_images

    try:
        microvm_images(settings, image="browser")
    except ConfigError as exc:
        raise ApiError("invalid_request", str(exc), code="invalid_request") from exc
