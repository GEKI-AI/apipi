from datetime import timedelta
from typing import Any

from apipi.config import Settings, parse_optional_ttl, parse_ttl
from apipi.gateway.errors import ApiError

IDLE_TTL_KEY = "apipi.idle_ttl"
IDLE_TTL_HELP = "idle_ttl must be like 15m or 0"
_OFF = frozenset({"0", "off", "false", "no"})


def normalize_idle_ttl(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("invalid_request", IDLE_TTL_HELP, code="invalid_request")
    raw = value.strip().lower()
    if raw == "":
        return None
    if raw in _OFF:
        return "0"
    try:
        parsed = parse_ttl(raw)
    except ValueError as exc:
        raise ApiError(
            "invalid_request", IDLE_TTL_HELP, code="invalid_request"
        ) from exc
    if not isinstance(parsed, timedelta) or parsed.total_seconds() < 0:
        raise ApiError("invalid_request", IDLE_TTL_HELP, code="invalid_request")
    return raw


def duration_of(stored: str) -> timedelta | None:
    if stored == "0":
        return None
    parsed = parse_optional_ttl(stored)
    if isinstance(parsed, timedelta):
        return parsed
    return None


def metadata_idle_ttl(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or IDLE_TTL_KEY not in metadata:
        return None
    return normalize_idle_ttl(metadata.get(IDLE_TTL_KEY))


def metadata_has_idle_ttl(metadata: dict[str, Any] | None) -> bool:
    return bool(metadata) and IDLE_TTL_KEY in metadata


def validate_idle_metadata(metadata: dict[str, Any] | None) -> None:
    if metadata_has_idle_ttl(metadata):
        metadata_idle_ttl(metadata)


def resolve_idle_ttl(
    settings: Settings,
    env_type: str | None,
    *,
    session_idle: str | None,
    session_metadata: dict[str, Any] | None,
    agent_idle: str | None,
) -> timedelta | None:
    if session_idle is not None:
        return duration_of(session_idle)
    if metadata_has_idle_ttl(session_metadata):
        stored = metadata_idle_ttl(session_metadata)
        if stored is not None:
            return duration_of(stored)
    if agent_idle is not None:
        return duration_of(agent_idle)
    return settings.pi_idle_ttl_for(env_type)
