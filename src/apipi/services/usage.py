import uuid
from datetime import datetime
from typing import Any

USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)

_PI_KEYS = {
    "prompt_tokens": ("prompt_tokens", "input"),
    "completion_tokens": ("completion_tokens", "output"),
    "cache_read_tokens": ("cache_read_tokens", "cacheRead"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWrite"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def empty_usage() -> dict[str, int]:
    return {field: 0 for field in USAGE_FIELDS}


def token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if value < 0:
        return 0
    return int(value)


def usage_from(raw: object | None) -> dict[str, int]:
    if not isinstance(raw, dict):
        return empty_usage()
    usage = empty_usage()
    for field, keys in _PI_KEYS.items():
        for key in keys:
            if key in raw:
                usage[field] = token_count(raw[key])
                break
    return usage


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {field: left.get(field, 0) + right.get(field, 0) for field in USAGE_FIELDS}


def usage_from_messages(messages: object) -> dict[str, int] | None:
    if not isinstance(messages, list):
        return None
    total = empty_usage()
    found = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        raw = message.get("usage")
        if not isinstance(raw, dict):
            continue
        total = add_usage(total, usage_from(raw))
        found = True
    if not found:
        return None
    return total


def usage_event(
    *,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    model: str | None,
    status: str,
    latency_ms: int,
    usage: dict[str, int],
    tool_names: list[str],
    tool_counts: dict[str, int],
    mcp_names: list[str],
    mcp_counts: dict[str, int],
    environment_type: str,
    run_mode: str,
    instance_id: str | None,
    artifact_bytes: int,
    request_id: str | None,
    error_code: str | None,
    created_at: datetime,
) -> dict[str, Any]:
    stored = usage_from(usage)
    return {
        "tenant_id": str(tenant_id),
        "key_id": key_id,
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "agent_id": str(agent_id) if agent_id is not None else None,
        "model": model,
        "status": status,
        "latency_ms": latency_ms,
        "prompt_tokens": stored["prompt_tokens"],
        "completion_tokens": stored["completion_tokens"],
        "cache_read_tokens": stored["cache_read_tokens"],
        "cache_write_tokens": stored["cache_write_tokens"],
        "total_tokens": stored["total_tokens"],
        "tool_names": list(tool_names),
        "tool_counts": dict(tool_counts),
        "mcp_names": list(mcp_names),
        "mcp_counts": dict(mcp_counts),
        "environment_type": environment_type,
        "run_mode": run_mode,
        "instance_id": instance_id,
        "artifact_bytes": artifact_bytes,
        "request_id": request_id,
        "error_code": error_code,
        "created_at": created_at.isoformat(),
    }
