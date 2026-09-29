from pathlib import Path

import pytest

from apipi.config import ConfigError, Settings
from apipi.worker.pi.fragments import (
    render_shipped,
    render_template,
    validate_fragments,
)
from apipi.worker.pi.platform_prompt import compose_instructions


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


def test_default_prompt_is_the_shipped_file() -> None:
    shipped = render_shipped("main.none", {"platform_name": "ApiPi"})
    assert compose_instructions(_settings(), None) == shipped
    assert "no computer" in shipped


def test_template_keeps_literal_braces_and_escape() -> None:
    assert (
        render_template("a {b} $${c}", {}, fragment="t", source="s", strict=True)
        == "a {b} ${c}"
    )


def test_missing_template_is_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apipi.worker.pi import fragments

    monkeypatch.setattr(fragments, "_PROMPTS", tmp_path)
    fragments._SHIPPED.clear()
    try:
        with pytest.raises(ConfigError, match="prompt template missing"):
            fragments.load_shipped()
    finally:
        fragments._SHIPPED.clear()


def test_unknown_variable_is_startup_error() -> None:
    with pytest.raises(ConfigError, match="unknown variable nope"):
        render_template("${nope}", {}, fragment="main.none", source="env", strict=True)


def test_inline_and_file_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIPI_PLATFORM_PROMPT_HOSTED", "hi")
    monkeypatch.setenv("APIPI_PLATFORM_PROMPT_HOSTED_FILE", "/tmp/x")
    with pytest.raises(ConfigError, match="not both"):
        validate_fragments(_settings())


def test_per_type_prompt_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIPI_PLATFORM_PROMPT_HOSTED", "hosted only")
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        platform_prompt="global",
    )
    hosted = compose_instructions(settings, None, env_type="openai_hosted")
    other = compose_instructions(settings, None, env_type="none")
    assert hosted is not None and hosted.startswith("hosted only")
    assert other is not None and other.startswith("global")


def test_platform_name_replaces_builtin_name() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        platform_name="Northstar",
    )
    text = compose_instructions(settings, None)
    assert text is not None
    assert "Northstar" in text
    assert "ApiPi" not in text


def test_instructions_are_not_templated() -> None:
    text = compose_instructions(_settings(), "use ${platform_name} literally")
    assert text is not None
    assert "${platform_name}" in text
