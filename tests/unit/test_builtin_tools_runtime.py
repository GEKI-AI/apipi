from apipi.services.runtime import (
    _cwd_and_tools,
    _effective_builtin_tools,
    _effective_codemode,
    _pi_spawn_overrides,
    _skill_dirs,
)


def test_effective_builtin_tools_defaults_on_for_hosted() -> None:
    assert _effective_builtin_tools({"type": "openai_hosted"}, {}, {}) == "on"
    assert (
        _effective_builtin_tools(
            {"type": "openai_hosted"},
            {"apipi.builtin_tools": "off"},
            {"apipi.builtin_tools": "on"},
        )
        == "off"
    )


def test_effective_builtin_tools_always_off_for_none() -> None:
    assert _effective_builtin_tools({"type": "none"}, {}, {}) == "off"
    assert (
        _effective_builtin_tools({"type": "none"}, {"apipi.builtin_tools": "off"}, None)
        == "off"
    )


def test_cwd_and_tools_follows_builtin_tools() -> None:
    env = {"type": "openai_hosted", "directory": "/tmp/ws"}
    assert _cwd_and_tools(env) == ("/tmp/ws", True)
    assert _cwd_and_tools(env, "off") == ("/tmp/ws", False)
    assert _cwd_and_tools({"type": "none"}) == (None, False)
    assert _cwd_and_tools({"type": "none"}, "on") == (None, False)


def test_skill_dirs_empty_when_builtin_tools_off(tmp_path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".agents" / "skills" / "demo").mkdir(parents=True)
    (workspace / ".agents" / "skills" / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n\nBody.\n"
    )
    env = {"type": "openai_hosted", "directory": str(workspace)}
    assert _skill_dirs(env, "off") == []
    assert _skill_dirs(env, "on") == [str(workspace / ".agents/skills/demo")]
    assert _skill_dirs({"type": "none"}) == []


def test_effective_codemode_forced_off_without_builtin_tools() -> None:
    assert _effective_codemode("off", {"apipi.codemode": "on"}, None) == "off"
    assert _effective_codemode("on", {"apipi.codemode": "on"}, None) == "on"
    assert _effective_codemode("on", {}, {}) == "off"


def test_spawn_overrides_force_codemode_off_without_builtin_tools() -> None:
    from apipi.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    overrides = _pi_spawn_overrides(settings, {"apipi.codemode": "on"}, None, "off")
    assert overrides["codemode"] == "off"
    overrides = _pi_spawn_overrides(settings, {"apipi.codemode": "on"}, None, "on")
    assert overrides["codemode"] == "on"
