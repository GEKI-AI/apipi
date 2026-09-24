import json
from pathlib import Path

import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.settings_json import (
    apply_pi_agent_files,
    resolve_system_prompt,
    resolve_thinking,
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
    assert payload["retry"] == {"enabled": False}
    assert payload["theme"] == "dark"
    written = json.loads(path.read_text())
    assert written["compaction"]["enabled"] is False
    assert (tmp_path / "SYSTEM.md").read_text() == "Be a custom harness.\n"


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
