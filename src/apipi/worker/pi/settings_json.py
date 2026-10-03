import json
from pathlib import Path
from typing import Any

from apipi.common.pi_metadata import SYSTEM_PROMPT_KEY, system_prompt_from_metadata
from apipi.config import Settings


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


def _delay_ms(base: int, attempt: int) -> int:
    delay = base
    for _ in range(max(attempt - 1, 0)):
        if delay > (1 << 62) // 2:
            return 1 << 62
        delay *= 2
    return delay


def capped_max_retries(settings: Settings) -> int:
    configured = settings.model_max_retries
    if not settings.model_retry_enabled or configured <= 0:
        return 0 if not settings.model_retry_enabled else configured
    base = settings.model_backoff_base_ms
    cap = settings.model_backoff_max_ms
    if base <= 0:
        return configured
    effective = configured
    while effective > 0 and _delay_ms(base, effective) > cap:
        effective -= 1
    return effective


def retry_budget_ms(settings: Settings) -> int:
    timeout = settings.model_timeout_ms
    if not settings.model_retry_enabled:
        return timeout
    retries = capped_max_retries(settings)
    backoff = sum(
        _delay_ms(settings.model_backoff_base_ms, attempt)
        for attempt in range(1, retries + 1)
    )
    return timeout * (retries + 1) + backoff


def model_retry_warnings(settings: Settings) -> list[str]:
    notes: list[str] = []
    if settings.model_retry_enabled:
        effective = capped_max_retries(settings)
        if effective < settings.model_max_retries:
            notes.append(
                "APIPI_MODEL_BACKOFF_MAX_MS capped retry.maxRetries "
                f"from {settings.model_max_retries} to {effective}"
            )
    budget = retry_budget_ms(settings)
    limit = int(settings.turn_timeout.total_seconds() * 1000)
    if budget > limit:
        notes.append(
            f"model retry budget {budget}ms exceeds APIPI_TURN_TIMEOUT {limit}ms"
        )
    return notes


def settings_payload(
    settings: Settings, *, thinking: str, codemode: str = "off"
) -> dict[str, Any]:
    compaction: dict[str, Any] = {"enabled": settings.pi_auto_compact}
    if settings.pi_compaction_reserve_tokens is not None:
        compaction["reserveTokens"] = settings.pi_compaction_reserve_tokens
    if settings.pi_compaction_keep_recent_tokens is not None:
        compaction["keepRecentTokens"] = settings.pi_compaction_keep_recent_tokens
    max_retries = (
        capped_max_retries(settings)
        if settings.model_retry_enabled
        else settings.model_max_retries
    )
    payload: dict[str, Any] = {
        "compaction": compaction,
        "defaultThinkingLevel": thinking,
        "defaultProjectTrust": "never",
        "httpIdleTimeoutMs": settings.model_timeout_ms,
        "retry": {
            "enabled": settings.model_retry_enabled,
            "maxRetries": max_retries,
            "baseDelayMs": settings.model_backoff_base_ms,
            "provider": {
                "maxRetries": settings.model_provider_retries,
                "maxRetryDelayMs": settings.model_retry_after_max_ms,
                "timeoutMs": settings.model_timeout_ms,
            },
        },
    }
    if codemode in ("on", "only"):
        payload["codemode"] = {"mode": codemode}
        payload["defaultTools"] = ["+codemode"]
    return payload


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
    settings: Settings,
    *,
    thinking: str,
    codemode: str = "off",
    current: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = dict(current or {})
    if codemode not in ("on", "only"):
        base.pop("codemode", None)
        base.pop("defaultTools", None)
    return _merge(
        base, settings_payload(settings, thinking=thinking, codemode=codemode)
    )


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
    env_type: str | None = None,
    codemode: str = "off",
) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "settings.json"
    payload = merged_settings(
        settings, thinking=thinking, codemode=codemode, current=_load_object(path)
    )
    path.write_text(settings_json_text(payload))
    write_system_prompt(directory, system_prompt)
    from apipi.worker.pi.fragments import (
        GUIDELINE_FILES,
        computer_identity,
        fragment_body,
        fragment_text,
    )

    kind = "identity.computer" if computer_identity(env_type) else "identity.none"
    identity = fragment_text(
        settings,
        kind,
        {"platform_name": settings.platform_name or "ApiPi"},
        strict=False,
    )
    (directory / "identity.txt").write_text(
        identity if identity.endswith("\n") else identity + "\n"
    )
    for name, filename in GUIDELINE_FILES.items():
        body = fragment_body(settings, name)
        text = body if body.endswith("\n") else body + "\n"
        (directory / filename).write_text(text)
    return payload
