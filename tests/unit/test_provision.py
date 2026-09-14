from pathlib import Path

import pytest

from apipi.env.setup import (
    ALPINE_HOSTS,
    NPM_HOSTS,
    PYPI_HOSTS,
    Packages,
    SetupCommand,
    SetupError,
    needs_setup,
    package_egress_hosts,
    packages_from,
    prepare_workspace,
    render_setup_script,
    resolve_setup_cwd,
    run_host_setup,
    setup_commands_from,
    workspace_egress_hosts,
)


def test_packages_from_empty() -> None:
    assert packages_from({}).empty()
    assert packages_from({"packages": {"python": []}}).empty()


def test_packages_from_lists() -> None:
    packages = packages_from(
        {"packages": {"python": ["pandas==2.2.3"], "npm": ["typescript"]}}
    )
    assert packages.python == ("pandas==2.2.3",)
    assert packages.npm == ("typescript",)
    assert packages.system == ()


def test_packages_reject_bad_name() -> None:
    with pytest.raises(SetupError, match="invalid package name"):
        packages_from({"packages": {"python": ["foo; rm"]}})


def test_setup_commands_from() -> None:
    commands = setup_commands_from(
        {"setup_commands": [{"command": "mkdir -p reports", "cwd": "out"}]}
    )
    assert commands == (SetupCommand(command="mkdir -p reports", cwd="out"),)


def test_needs_setup() -> None:
    assert not needs_setup({"type": "openai_hosted"})
    assert needs_setup({"type": "openai_hosted", "packages": {"python": ["rich"]}})
    assert not needs_setup({"type": "none", "packages": {"python": ["rich"]}})


def test_package_egress_hosts_by_kind() -> None:
    assert package_egress_hosts({"packages": {"python": ["rich"]}}) == PYPI_HOSTS
    assert package_egress_hosts({"packages": {"npm": ["typescript"]}}) == NPM_HOSTS
    assert package_egress_hosts({"packages": {"system": ["git"]}}) == ALPINE_HOSTS
    assert package_egress_hosts({}) == ()


def test_resolve_setup_cwd_maps_openai_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    assert resolve_setup_cwd(workspace, None) == workspace.resolve()
    assert resolve_setup_cwd(workspace, "/workspace") == workspace.resolve()
    assert (
        resolve_setup_cwd(workspace, "/tmp/workspace/out")
        == (workspace / "out").resolve()
    )
    nested = resolve_setup_cwd(workspace, "reports")
    assert nested == (workspace / "reports").resolve()
    with pytest.raises(SetupError, match="inside the workspace"):
        resolve_setup_cwd(workspace, "/etc")


def test_render_setup_script_installs_and_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    script = render_setup_script(
        workspace,
        Packages(python=("pandas==2.2.3",), npm=("typescript",)),
        (SetupCommand(command="mkdir -p reports"),),
    )
    assert "uv pip install" in script
    assert "pandas==2.2.3" in script
    assert "npm install -g" in script
    assert "typescript" in script
    assert "mkdir -p reports" in script


def test_prepare_and_run_host_setup(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    prepare_workspace(
        workspace,
        {
            "type": "openai_hosted",
            "setup_commands": [{"command": "mkdir -p reports && echo ok > reports/hi"}],
        },
    )
    assert (workspace / ".apipi" / "setup.sh").is_file()
    run_host_setup(workspace)
    assert (workspace / "reports" / "hi").read_text() == "ok\n"
    assert (workspace / ".apipi" / "setup.done").is_file()
    (workspace / "reports" / "hi").write_text("again")
    run_host_setup(workspace)
    assert (workspace / "reports" / "hi").read_text() == "again"


def test_run_host_setup_nonzero_is_setup_error(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    prepare_workspace(
        workspace,
        {
            "type": "openai_hosted",
            "setup_commands": [{"command": "exit 1"}],
        },
    )
    with pytest.raises(SetupError):
        run_host_setup(workspace)
    assert not (workspace / ".apipi" / "setup.done").is_file()


def test_guest_run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from apipi.pi.guest import _run_setup

    workspace = tmp_path / "guest"
    workspace.mkdir()
    prepare_workspace(
        workspace,
        {
            "type": "openai_hosted",
            "setup_commands": [{"command": "echo guest > marker"}],
        },
    )
    monkeypatch.setenv("HOME", str(workspace))
    _run_setup()
    assert (workspace / "marker").read_text() == "guest\n"
    _run_setup()
    assert (workspace / ".apipi" / "setup.done").is_file()


def test_workspace_egress_hosts_file(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    prepare_workspace(
        workspace,
        {"type": "openai_hosted", "packages": {"python": ["rich"]}},
    )
    hosts = workspace_egress_hosts(str(workspace))
    assert "pypi.org" in hosts
    assert workspace_egress_hosts(None) == []
