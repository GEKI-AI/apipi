import json
from datetime import timedelta
from pathlib import Path

import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.settings_json import (
    apply_pi_agent_files,
    capped_max_retries,
    model_retry_warnings,
    resolve_system_prompt,
    resolve_thinking,
    retry_budget_ms,
    settings_payload,
    validate_pi_metadata,
)


def _settings(**updates: object) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    if not updates:
        return base
    return base.model_copy(update=updates)


def test_merge_keeps_unknown_keys_and_disables_compaction(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"retry": {"enabled": False}, "theme": "dark"}) + "\n")
    payload = apply_pi_agent_files(
        tmp_path,
        _settings(pi_auto_compact=False, pi_compaction_reserve_tokens=8192),
        thinking="high",
        system_prompt="Be a custom harness.",
    )
    assert payload["compaction"] == {"enabled": False, "reserveTokens": 8192}
    assert payload["defaultThinkingLevel"] == "high"
    assert payload["httpIdleTimeoutMs"] == 120000
    assert payload["retry"]["enabled"] is True
    assert payload["retry"]["maxRetries"] == 3
    assert payload["retry"]["baseDelayMs"] == 2000
    assert payload["retry"]["provider"] == {
        "maxRetries": 0,
        "maxRetryDelayMs": 30000,
        "timeoutMs": 120000,
    }
    assert payload["theme"] == "dark"
    written = json.loads(path.read_text())
    assert written["compaction"]["enabled"] is False
    assert (tmp_path / "SYSTEM.md").read_text() == "Be a custom harness.\n"


def test_identity_follows_environment(tmp_path: Path) -> None:
    apply_pi_agent_files(
        tmp_path / "none",
        _settings(platform_name="GEKI"),
        thinking="off",
        system_prompt=None,
        env_type="none",
    )
    apply_pi_agent_files(
        tmp_path / "hosted",
        _settings(platform_name="GEKI"),
        thinking="off",
        system_prompt=None,
        env_type="openai_hosted",
    )
    none = (tmp_path / "none" / "identity.txt").read_text()
    hosted = (tmp_path / "hosted" / "identity.txt").read_text()
    assert none == "You are a GEKI agent using Pi as your harness.\n"
    assert "sandbox" not in none
    assert hosted == (
        "You are a GEKI agent running in a sandbox using Pi as your harness.\n"
    )
    assert "Use ${tool_name}" in (tmp_path / "none" / "mcp-tool.txt").read_text()


def test_unset_system_prompt_removes_stale_file(tmp_path: Path) -> None:
    (tmp_path / "SYSTEM.md").write_text("old\n")
    apply_pi_agent_files(
        tmp_path,
        _settings(),
        thinking="off",
        system_prompt=None,
    )
    assert not (tmp_path / "SYSTEM.md").exists()
    doc = json.loads((tmp_path / "settings.json").read_text())
    assert doc["compaction"]["enabled"] is True
    assert "reserveTokens" not in doc["compaction"]


def test_reasoning_effort_mirrors_thinking() -> None:
    from apipi.worker.pi.settings_json import (
        apply_reasoning_effort,
        reasoning_body,
        reject_reasoning_conflict,
    )

    stored = apply_reasoning_effort({}, "high")
    assert stored["apipi.thinking"] == "high"
    assert reasoning_body(stored) == {"effort": "high"}
    reset = apply_reasoning_effort(stored, None, reset=True)
    assert "apipi.thinking" not in reset
    replaced = apply_reasoning_effort({"apipi.thinking": "low", "team": "x"}, "high")
    assert replaced["apipi.thinking"] == "high"
    assert replaced["team"] == "x"
    with pytest.raises(ApiError, match="disagree"):
        reject_reasoning_conflict({"apipi.thinking": "low"}, "high")


def test_thinking_level_map_matches_pi() -> None:
    from apipi.worker.pi.settings_json import (
        require_thinking_supported,
        thinking_level_supported,
    )

    levels = {"high": "high", "minimal": None}
    assert thinking_level_supported(levels, "high")
    assert not thinking_level_supported(levels, "minimal")
    assert thinking_level_supported(levels, "low")
    assert thinking_level_supported(levels, "off")
    assert not thinking_level_supported(levels, "xhigh")
    assert thinking_level_supported({"xhigh": "xhigh"}, "xhigh")
    assert not thinking_level_supported({"off": None}, "off")
    assert thinking_level_supported(None, "max")
    settings = _settings(
        model_registry={"m": {"thinking_levels": levels, "reasoning": True}}
    )
    require_thinking_supported(settings, "m", "low")
    require_thinking_supported(settings, "m", "off")
    with pytest.raises(ApiError):
        require_thinking_supported(settings, "m", "minimal")
    with pytest.raises(ApiError):
        require_thinking_supported(settings, "m", "max")


def test_thinking_resolve_session_over_agent() -> None:
    settings = _settings(pi_thinking="off")
    assert (
        resolve_thinking(
            settings,
            {"apipi.thinking": "low"},
            {"apipi.thinking": "high"},
        )
        == "low"
    )
    assert resolve_thinking(settings, {}, {"apipi.thinking": "high"}) == "high"
    assert resolve_thinking(settings, {}, {}) == "off"


def test_system_prompt_empty_session_does_not_use_agent() -> None:
    settings = _settings(pi_system_prompt="process")
    assert (
        resolve_system_prompt(
            settings,
            {"apipi.system_prompt": ""},
            {"apipi.system_prompt": "agent"},
        )
        is None
    )
    assert resolve_system_prompt(settings, {}, {"apipi.system_prompt": "agent"}) == (
        "agent"
    )
    assert resolve_system_prompt(settings, {}, {}) == "process"


def test_invalid_thinking_rejected() -> None:
    with pytest.raises(ApiError) as exc:
        validate_pi_metadata({"apipi.thinking": "ultra"})
    assert exc.value.status_code == 400
    with pytest.raises(ApiError):
        validate_pi_metadata({"apipi.system_prompt": 1})


def test_settings_payload_writes_retry_and_timeout() -> None:
    payload = settings_payload(
        _settings(
            model_retry_enabled=True,
            model_max_retries=2,
            model_backoff_base_ms=1000,
            model_backoff_max_ms=30000,
            model_timeout_ms=1500,
            model_provider_retries=1,
            model_retry_after_max_ms=5000,
        ),
        thinking="off",
    )
    assert payload["httpIdleTimeoutMs"] == 1500
    assert payload["retry"] == {
        "enabled": True,
        "maxRetries": 2,
        "baseDelayMs": 1000,
        "provider": {
            "maxRetries": 1,
            "maxRetryDelayMs": 5000,
            "timeoutMs": 1500,
        },
    }


def test_backoff_max_caps_effective_retries() -> None:
    settings = _settings(
        model_retry_enabled=True,
        model_max_retries=5,
        model_backoff_base_ms=2000,
        model_backoff_max_ms=30000,
    )
    assert capped_max_retries(settings) == 4
    payload = settings_payload(settings, thinking="off")
    assert payload["retry"]["maxRetries"] == 4
    notes = model_retry_warnings(settings)
    assert any("capped retry.maxRetries from 5 to 4" in note for note in notes)


def test_retry_budget_warns_when_over_turn_timeout() -> None:
    settings = _settings(
        model_timeout_ms=120000,
        model_max_retries=3,
        model_backoff_base_ms=2000,
        turn_timeout=timedelta(minutes=10),
    )
    assert retry_budget_ms(settings) < 10 * 60 * 1000
    assert model_retry_warnings(settings) == []
    over = settings.model_copy(update={"model_timeout_ms": 200000})
    assert model_retry_warnings(over)
