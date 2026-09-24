import json
from pathlib import Path
from typing import Any

from apipi.config import Settings
from apipi.gateway.errors import ApiError

THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
THINKING_KEY = "apipi.thinking"
SYSTEM_PROMPT_KEY = "apipi.system_prompt"
THINKING_HELP = "apipi.thinking must be off, minimal, low, medium, high, xhigh, or max"
SYSTEM_PROMPT_HELP = "apipi.system_prompt must be a string"


def parse_thinking(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in THINKING_LEVELS:
        raise ApiError("invalid_request", THINKING_HELP, code="invalid_request")
    return value


def thinking_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or THINKING_KEY not in metadata:
        return None
    return parse_thinking(metadata.get(THINKING_KEY))


def parse_system_prompt(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError("invalid_request", SYSTEM_PROMPT_HELP, code="invalid_request")
    text = value.strip()
    return text or None


def system_prompt_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or SYSTEM_PROMPT_KEY not in metadata:
        return None
    return parse_system_prompt(metadata.get(SYSTEM_PROMPT_KEY))


def validate_pi_metadata(metadata: dict[str, Any] | None) -> None:
    thinking_from_metadata(metadata)
    system_prompt_from_metadata(metadata)


def copy_inline_pi_metadata(
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    out = dict(session_metadata or {})
    agent = agent_metadata or {}
    for key in (THINKING_KEY, SYSTEM_PROMPT_KEY):
        if key not in out and key in agent:
            out[key] = agent[key]
    return out


def resolve_thinking(
    settings: Settings,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    session = thinking_from_metadata(session_metadata)
    if session is not None:
        return session
    agent = thinking_from_metadata(agent_metadata)
    if agent is not None:
        return agent
    return settings.pi_thinking


def resolve_system_prompt(
    settings: Settings,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str | None:
    if session_metadata and SYSTEM_PROMPT_KEY in session_metadata:
        return system_prompt_from_metadata(session_metadata)
    if agent_metadata and SYSTEM_PROMPT_KEY in agent_metadata:
        return system_prompt_from_metadata(agent_metadata)
    return process_system_prompt(settings)


def process_system_prompt(settings: Settings) -> str | None:
    raw = settings.pi_system_prompt
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def settings_payload(settings: Settings, *, thinking: str) -> dict[str, Any]:
    compaction: dict[str, Any] = {"enabled": settings.pi_auto_compact}
    if settings.pi_compaction_reserve_tokens is not None:
        compaction["reserveTokens"] = settings.pi_compaction_reserve_tokens
    if settings.pi_compaction_keep_recent_tokens is not None:
        compaction["keepRecentTokens"] = settings.pi_compaction_keep_recent_tokens
    return {
        "compaction": compaction,
        "defaultThinkingLevel": thinking,
    }


def _load_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        current = out.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            out[key] = _merge(current, value)
        else:
            out[key] = value
    return out


def merged_settings(
    settings: Settings, *, thinking: str, current: dict[str, Any] | None = None
) -> dict[str, Any]:
    return _merge(current or {}, settings_payload(settings, thinking=thinking))


def settings_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2) + "\n"


def write_system_prompt(directory: Path, text: str | None) -> None:
    path = directory / "SYSTEM.md"
    if text:
        body = text if text.endswith("\n") else text + "\n"
        path.write_text(body)
        return
    if path.is_file():
        path.unlink()


def apply_pi_agent_files(
    directory: Path,
    settings: Settings,
    *,
    thinking: str,
    system_prompt: str | None,
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.json"
    payload = merged_settings(settings, thinking=thinking, current=_load_object(path))
    path.write_text(settings_json_text(payload))
    write_system_prompt(directory, system_prompt)
    return payload
