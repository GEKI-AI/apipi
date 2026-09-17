import base64
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
    provision_hosted,
    render_setup_script,
    resolve_setup_cwd,
    run_host_setup,
    session_env_from,
    session_network_from,
    setup_commands_from,
    tap_policy_from,
    workspace_egress_hosts,
    workspace_network_policy,
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
    from apipi.worker.pi.guest import _run_setup

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


def test_session_env_rejects_reserved() -> None:
    with pytest.raises(SetupError, match="reserved env"):
        session_env_from({"env": {"PATH": "/bin"}})
    with pytest.raises(SetupError, match="reserved env"):
        session_env_from({"env": {"APIPI_FOO": "x"}})
    assert session_env_from({"env": {"REPORT": "yes"}}) == {"REPORT": "yes"}


def test_prepare_writes_inline_files_and_env(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    payload = base64.b64encode(b"hello").decode()
    prepare_workspace(
        workspace,
        {
            "type": "openai_hosted",
            "env": {"REPORT": "yes"},
            "files": [
                {"type": "inline", "path": "/workspace/notes.txt", "data": payload}
            ],
        },
    )
    assert (workspace / "notes.txt").read_bytes() == b"hello"
    assert "REPORT=" in (workspace / ".apipi" / "user.env").read_text()
    assert not (workspace / ".apipi" / "setup.sh").exists()


def test_inline_file_rejects_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    payload = base64.b64encode(b"x").decode()
    with pytest.raises(SetupError, match="inside the workspace"):
        prepare_workspace(
            workspace,
            {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "inline",
                        "path": "/etc/passwd",
                        "data": payload,
                    }
                ],
            },
        )


def test_session_network_from_shapes() -> None:
    assert session_network_from({}) is None
    disabled = session_network_from({"network": {"access": "disabled"}})
    assert disabled is not None
    assert disabled.access == "disabled"
    policy = session_network_from(
        {"network": {"access": "restricted", "allowed_domains": ["api.example.com"]}}
    )
    assert policy is not None
    assert policy.allowed_domains == ("api.example.com",)
    with pytest.raises(SetupError, match="allowed_domains"):
        session_network_from({"network": {"access": "restricted"}})
    with pytest.raises(SetupError, match="only valid with restricted"):
        session_network_from(
            {"network": {"access": "enabled", "allowed_domains": ["api.example.com"]}}
        )
    with pytest.raises(SetupError, match="invalid network host"):
        session_network_from(
            {
                "network": {
                    "access": "restricted",
                    "allowed_domains": ["https://api.example.com"],
                }
            }
        )
    with pytest.raises(SetupError, match="invalid network host"):
        session_network_from(
            {"network": {"access": "restricted", "allowed_domains": ["*.example.com"]}}
        )


def test_tap_policy_gateway_floor() -> None:
    restricted = session_network_from(
        {"network": {"access": "restricted", "allowed_domains": ["api.example.com"]}}
    )
    assert restricted is not None
    open_tap = tap_policy_from(
        restricted, gateway_allowlist=False, extra_hosts=PYPI_HOSTS
    )
    assert open_tap.allowlist is True
    assert "api.example.com" in open_tap.hosts
    assert "pypi.org" in open_tap.hosts
    with pytest.raises(SetupError, match="not allowed"):
        tap_policy_from(
            restricted,
            gateway_allowlist=True,
            gateway_hosts=("mcp.tavily.com",),
        )
    ok = tap_policy_from(
        restricted,
        gateway_allowlist=True,
        gateway_hosts=("api.example.com",),
    )
    assert ok.hosts == ("api.example.com",)
    disabled = session_network_from({"network": {"access": "disabled"}})
    assert disabled is not None
    locked = tap_policy_from(disabled, gateway_allowlist=False, extra_hosts=PYPI_HOSTS)
    assert locked.allowlist is True
    assert locked.hosts == ()
    enabled_policy = session_network_from({"network": {"access": "enabled"}})
    enabled = tap_policy_from(
        enabled_policy,
        gateway_allowlist=True,
        gateway_hosts=("api.openai.com",),
        extra_hosts=("pypi.org",),
    )
    assert enabled.allowlist is True
    assert enabled.hosts == ("api.openai.com", "pypi.org")
    assert tap_policy_from(None, gateway_allowlist=False).allowlist is False


def test_prepare_writes_network_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    prepare_workspace(
        workspace,
        {
            "type": "openai_hosted",
            "network": {
                "access": "restricted",
                "allowed_domains": ["api.example.com"],
            },
        },
    )
    policy = workspace_network_policy(str(workspace))
    assert policy is not None
    assert policy.access == "restricted"
    assert policy.allowed_domains == ("api.example.com",)


def test_provision_network_disabled_on_none(tmp_path: Path) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    with pytest.raises(SetupError, match="microvm isolation"):
        provision_hosted(
            {
                "type": "openai_hosted",
                "directory": str(workspace),
                "network": {"access": "disabled"},
            },
            run_mode="none",
        )
