from apipi.config import Settings
from apipi.pi.platform_prompt import (
    BROWSER_HINT,
    DEFAULT_PLATFORM_PROMPT,
    compose_instructions,
    sandbox_size_hint,
)


def _settings(
    *,
    platform_prompt: str | None = None,
    platform_prompt_additional: str = "",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        platform_prompt=platform_prompt,
        platform_prompt_additional=platform_prompt_additional,
    )


def test_default_main_then_agent() -> None:
    assert compose_instructions(_settings(), "be brief") == (
        f"{DEFAULT_PLATFORM_PROMPT}\n\nbe brief"
    )


def test_omitted_agent_keeps_default_main() -> None:
    assert compose_instructions(_settings(), None) == DEFAULT_PLATFORM_PROMPT
    assert compose_instructions(_settings(), "") == DEFAULT_PLATFORM_PROMPT


def test_empty_main_keeps_additional_and_agent() -> None:
    settings = _settings(
        platform_prompt="",
        platform_prompt_additional="Always answer in German.",
    )
    assert compose_instructions(settings, "be brief") == (
        "Always answer in German.\n\nbe brief"
    )


def test_empty_main_without_other_blocks_is_none() -> None:
    assert compose_instructions(_settings(platform_prompt=""), None) is None


def test_override_main_then_additional_then_agent() -> None:
    settings = _settings(
        platform_prompt="Use outputs/ only.",
        platform_prompt_additional="Be terse.",
    )
    assert compose_instructions(settings, "write tests") == (
        "Use outputs/ only.\n\nBe terse.\n\nwrite tests"
    )


def test_additional_without_touching_main() -> None:
    settings = _settings(platform_prompt_additional="Be terse.")
    assert compose_instructions(settings, None) == (
        f"{DEFAULT_PLATFORM_PROMPT}\n\nBe terse."
    )


def test_default_mentions_outputs_not_workspace_artifacts() -> None:
    assert "outputs/" in DEFAULT_PLATFORM_PROMPT
    assert "artifacts/" not in DEFAULT_PLATFORM_PROMPT


def test_browser_hint_only_when_requested() -> None:
    plain = compose_instructions(_settings(), None)
    assert plain is not None
    assert BROWSER_HINT not in plain
    with_browser = compose_instructions(_settings(), None, browser=True)
    assert with_browser is not None
    assert with_browser.endswith(BROWSER_HINT)
    assert DEFAULT_PLATFORM_PROMPT in with_browser


def test_sandbox_size_hint_l_forbids_install() -> None:
    text = sandbox_size_hint("L")
    assert "Sandbox size is L" in text
    assert "Do not install Playwright" in text
    composed = compose_instructions(_settings(), None, sandbox_size="L", browser=True)
    assert composed is not None
    assert "Sandbox size is L" in composed
    assert BROWSER_HINT in composed


def test_sandbox_size_hint_s_has_no_browser() -> None:
    text = sandbox_size_hint("S")
    assert "Sandbox size is S" in text
    assert "no browser" in text
