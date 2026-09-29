import json
from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.platform_prompt import (
    HOSTED_PROMPT,
    NO_COMPUTER_PROMPT,
    SELF_HOSTED_PROMPT,
    compose_instructions,
    sandbox_size_hint,
)


def _settings(
    *,
    platform_prompt: str | None = None,
    platform_prompt_additional: str = "",
    run_mode: str = "none",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        platform_prompt=platform_prompt,
        platform_prompt_additional=platform_prompt_additional,
    )


def test_default_main_then_agent() -> None:
    assert compose_instructions(_settings(), "be brief") == (
        f"{NO_COMPUTER_PROMPT}\n\nbe brief"
    )


def test_omitted_agent_keeps_default_main() -> None:
    assert compose_instructions(_settings(), None) == NO_COMPUTER_PROMPT
    assert compose_instructions(_settings(), "") == NO_COMPUTER_PROMPT


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
        f"{NO_COMPUTER_PROMPT}\n\nBe terse."
    )


def test_none_and_chat_omit_workspace_and_size() -> None:
    settings = _settings(run_mode="microvm")
    for env_type, chat in (("none", False), ("openai_hosted", True), (None, True)):
        text = compose_instructions(
            settings,
            None,
            env_type=env_type,
            chat=chat,
            sandbox_size="L",
            mem_mib=2048,
        )
        assert text == NO_COMPUTER_PROMPT
        assert text is not None
        assert "/workspace" not in text
        assert "Sandbox size" not in text
        assert "Chromium" not in text


def test_hosted_prompt_names_workspace_not_size_on_none() -> None:
    text = compose_instructions(
        _settings(),
        None,
        env_type="openai_hosted",
        sandbox_size="L",
        mem_mib=2048,
    )
    assert text == HOSTED_PROMPT
    assert text is not None
    assert "/workspace" in text
    assert "Sandbox size" not in text
    assert "Chromium" not in text
    assert "Playwright" not in text


def test_self_hosted_prompt_names_runner_files() -> None:
    text = compose_instructions(_settings(), None, env_type="self_hosted")
    assert text == SELF_HOSTED_PROMPT
    assert text is not None
    assert "runner's files" in text
    assert "/workspace" not in text


def test_microvm_size_hint_is_ram_only() -> None:
    text = sandbox_size_hint("L", 2048)
    assert text == "Sandbox size is L (2048 MiB)."
    assert "Chromium" not in text
    assert "browser" not in text
    composed = compose_instructions(
        _settings(run_mode="microvm"),
        None,
        env_type="openai_hosted",
        sandbox_size="L",
        mem_mib=2048,
        network="enabled",
    )
    assert composed is not None
    assert "Sandbox size is L (2048 MiB)." not in composed
    assert "This sandbox has network access." not in composed
    assert "inputs/" in composed
    assert "Today is" not in composed
    assert "Chromium" not in composed
    assert "Playwright" not in composed


def test_restricted_network_hint() -> None:
    text = compose_instructions(
        _settings(run_mode="microvm"),
        None,
        env_type="hosted",
        sandbox_size="M",
        mem_mib=1024,
        network="restricted",
    )
    assert text is not None
    assert "restricted network access" not in text
    assert "Sandbox size is M (1024 MiB)." not in text
    assert "idle or TTL stop" in text


def test_capability_file_has_no_date(tmp_path: Path) -> None:
    text = compose_instructions(
        _settings(run_mode="microvm"),
        None,
        env_type="openai_hosted",
        sandbox_size="M",
        mem_mib=1024,
        network="enabled",
        image="work",
        vcpus=2,
        cwd=str(tmp_path),
    )
    assert text is not None
    assert "Today is" not in text
    assert "Do not assume a browser is available" not in text
    payload = json.loads((tmp_path / ".pi" / "agent" / "capability.json").read_text())
    assert "Do not assume a browser is available" in payload["block"]
    assert "Chromium is" not in payload["block"]
    assert "work" in payload["block"]
    assert "1024" in payload["block"]


def test_disabled_or_unset_network_is_omitted() -> None:
    for network in (None, "disabled"):
        text = compose_instructions(
            _settings(run_mode="microvm"),
            None,
            env_type="openai_hosted",
            sandbox_size="S",
            mem_mib=512,
            network=network,
        )
        assert text is not None
        assert "network" not in text
