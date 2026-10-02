import json
from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.fragments import render_shipped
from apipi.worker.pi.platform_prompt import compose_instructions


def _main(kind: str) -> str:
    return render_shipped(
        f"main.{kind}",
        {
            "platform_name": "ApiPi",
            "workspace": "/workspace" if kind == "hosted" else "",
        },
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
        f"{_main('none')}\n\nbe brief"
    )


def test_omitted_agent_keeps_default_main() -> None:
    assert compose_instructions(_settings(), None) == _main("none")
    assert compose_instructions(_settings(), "") == _main("none")


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
    assert compose_instructions(settings, None) == (f"{_main('none')}\n\nBe terse.")


def test_none_omits_workspace() -> None:
    settings = _settings(run_mode="microvm")
    for env_type in ("none", "openai_hosted", None):
        text = compose_instructions(
            settings,
            None,
            env_type=env_type,
            sandbox_size="L",
            mem_mib=2048,
        )
        if env_type == "openai_hosted":
            assert text is not None
            assert "/workspace" in text
        else:
            assert text == _main("none")
            assert text is not None
            assert "/workspace" not in text


def test_hosted_prompt_names_workspace() -> None:
    text = compose_instructions(
        _settings(),
        None,
        env_type="openai_hosted",
        sandbox_size="L",
        mem_mib=2048,
    )
    assert text == _main("hosted")
    assert "idle time" in text
    assert "inputs/" in text
    assert "non-persistent" in text
    assert "outputs/" in text
    assert text is not None
    assert "/workspace" in text


def test_builtin_tools_off_uses_none_fragment() -> None:
    text = compose_instructions(
        _settings(),
        None,
        env_type="openai_hosted",
        sandbox_size="L",
        mem_mib=2048,
        builtin_tools="off",
    )
    assert text == _main("none")
    assert text is not None
    assert "/workspace" not in text
    assert "outputs/" not in text


def test_builtin_tools_off_skips_capability_file(tmp_path: Path) -> None:
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
        builtin_tools="off",
    )
    assert text is not None
    assert "/workspace" not in text
    assert not (tmp_path / ".pi" / "agent" / "capability.json").exists()


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
    assert "Today is ${date}." in payload["block"]
    assert "work" in payload["block"]
    assert "1024" in payload["block"]
