import asyncio
import json
import tarfile
from pathlib import Path
from typing import Any, cast

import pytest

from apipi.config import (
    IMPLEMENTED_RUN_MODES,
    ConfigError,
    RunMode,
    Settings,
    require_run_mode,
)
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.artifacts import unpack_workspace_tar
from apipi.pi.guest import _pi_args, _start_mcp, workspace_tar_bytes
from apipi.pi.microvm import (
    BOOT_ARGS,
    GUEST_DNS,
    GUEST_WORKSPACE,
    VSOCK_PORT,
    connect_vsock,
    egress_host,
    guest_env,
    guest_skill_dirs,
    jailer_argv,
    microvm_config,
    microvm_egress_hosts,
    require_microvm,
    resolve_host_ips,
    setup_tap,
    spawn_microvm_pi,
    tap_net,
    tap_setup_argv,
    tap_teardown_argv,
    write_workspace_image,
)
from apipi.pi.proc import PiProc, spawn_pi


def _settings(
    tmp_path: Path,
    *,
    run_mode: RunMode = "microvm",
    kernel: str | None = None,
    rootfs: str | None = None,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        pi_command="pi",
        microvm_kernel=kernel or str(tmp_path / "vmlinux"),
        microvm_rootfs=rootfs or str(tmp_path / "rootfs.ext4"),
    )


def _images(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    kernel.write_bytes(b"k")
    rootfs.write_bytes(b"r")
    return kernel, rootfs


def _which_ok(name: str) -> str:
    return f"/usr/bin/{name}"


def test_microvm_is_implemented() -> None:
    assert "microvm" in IMPLEMENTED_RUN_MODES


def test_require_microvm_missing_kvm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: False)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_microvm()
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_run_mode("microvm")


def test_require_microvm_missing_firecracker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "firecracker" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="firecracker"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_jailer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "jailer" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="jailer"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.write_bytes(b"r")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_KERNEL"):
        require_microvm(
            _settings(tmp_path, kernel=str(tmp_path / "missing"), rootfs=str(rootfs))
        )


def test_require_microvm_missing_rootfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"k")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_ROOTFS"):
        require_microvm(
            _settings(tmp_path, kernel=str(kernel), rootfs=str(tmp_path / "missing"))
        )


def test_require_microvm_missing_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "ip" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match=r"requires ip$"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_iptables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "iptables" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="requires iptables"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_tc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "tc" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="requires tc"):
        require_microvm(_settings(tmp_path))


def test_jailer_argv_and_config_use_vsock() -> None:
    argv = jailer_argv(
        jailer="/usr/bin/jailer",
        firecracker="/usr/bin/firecracker",
        vm_id="551e7604-e35c-42b3-b825-416853441234",
        uid=123,
        gid=100,
        chroot_base="/tmp/apipi-microvm",
    )
    assert argv[0] == "/usr/bin/jailer"
    assert "--exec-file" in argv
    assert argv[argv.index("--exec-file") + 1] == "/usr/bin/firecracker"
    assert "--" in argv
    assert argv[argv.index("--") + 1 :] == ["--no-api", "--config-file", "config.json"]
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    config = microvm_config(
        kernel="vmlinux",
        rootfs="rootfs.ext4",
        workspace="workspace.tar",
        vsock="vsock.sock",
        cid=3,
        net=net,
    )
    assert config["vsock"] == {"guest_cid": 3, "uds_path": "vsock.sock"}
    boot = cast(dict[str, str], config["boot-source"])
    assert boot["kernel_image_path"] == "vmlinux"
    assert "init=/sbin/apipi-guest" in BOOT_ARGS
    assert boot["boot_args"].startswith(BOOT_ARGS)
    assert f"ip={net.guest_ip}::{net.host_ip}:" in boot["boot_args"]
    drives = cast(list[dict[str, object]], config["drives"])
    assert drives[0]["path_on_host"] == "rootfs.ext4"
    assert drives[1]["path_on_host"] == "workspace.tar"
    nics = cast(list[dict[str, str]], config["network-interfaces"])
    assert nics == [
        {
            "iface_id": "eth0",
            "guest_mac": net.mac,
            "host_dev_name": net.name,
        }
    ]


def test_workspace_image_has_env_and_session(tmp_path: Path) -> None:
    cwd = tmp_path / "session"
    cwd.mkdir()
    (cwd / "note.txt").write_text("hello")
    dest = tmp_path / "workspace.tar"
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    write_workspace_image(
        dest,
        cwd=str(cwd),
        env={"OPENAI_API_KEY": "k", "DATABASE_URL": "postgresql://x"},
        pi_args=["pi", "--mode", "rpc", "--no-session"],
        net=net,
    )
    with tarfile.open(dest, mode="r") as tar:
        names = tar.getnames()
        assert any(name.endswith("note.txt") for name in names)
        assert any(
            name.endswith(".apipi/env") or name == ".apipi/env" for name in names
        )
        assert any(name.endswith("guest.py") for name in names)
        env = tar.extractfile(".apipi/env")
        assert env is not None
        text = env.read().decode()
        net_f = tar.extractfile(".apipi/net")
        assert net_f is not None
        net_text = net_f.read().decode()
    assert "OPENAI_API_KEY" in text
    assert "DATABASE_URL" not in text
    assert net.guest_ip in net_text
    assert net.host_ip in net_text
    assert "127.0.0.1" not in net_text


def test_guest_workspace_pull_is_source_for_next_pack(tmp_path: Path) -> None:
    guest = tmp_path / "guest"
    guest.mkdir()
    (guest / "keep.txt").write_text("from-guest")
    (guest / ".apipi").mkdir()
    (guest / ".apipi" / "env").write_text("secret")
    host = tmp_path / "session"
    unpack_workspace_tar(workspace_tar_bytes(guest), host)
    dest = tmp_path / "workspace.tar"
    write_workspace_image(
        dest,
        cwd=str(host),
        env={},
        pi_args=["pi", "--mode", "rpc", "--no-session"],
    )
    with tarfile.open(dest, mode="r") as tar:
        names = tar.getnames()
        member = next(name for name in names if name.endswith("keep.txt"))
        keep = tar.extractfile(member)
        assert keep is not None
        assert keep.read() == b"from-guest"
    assert (host / "keep.txt").read_text() == "from-guest"
    assert not (host / ".apipi").exists()


def test_tap_setup_nat_without_host_loopback() -> None:
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    argv = tap_setup_argv(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        uid=123,
        gid=100,
        tc="/sbin/tc",
        allowed_ips=["203.0.113.10"],
    )
    flat = " ".join(" ".join(part) for part in argv)
    assert net.name in flat
    assert "tuntap" in flat
    assert "MASQUERADE" in flat
    assert "127.0.0.1" not in flat
    assert "DNAT" not in flat
    assert "--map-host-loopback" not in flat
    assert "REJECT" in flat
    assert "203.0.113.10" in flat
    assert "50mbit" in flat
    for dns in GUEST_DNS:
        assert dns in flat
    assert "198.51.100.9" not in flat


def test_tap_setup_allowlist_off_accepts_all() -> None:
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    argv = tap_setup_argv(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        uid=123,
        gid=100,
        allowlist=False,
    )
    flat = " ".join(" ".join(part) for part in argv)
    assert "-j ACCEPT" in flat
    assert "REJECT" not in flat


def test_tap_teardown_cleans_tc_and_chain() -> None:
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    argv = tap_teardown_argv(
        net, ip="/sbin/ip", iptables="/sbin/iptables", tc="/sbin/tc"
    )
    flat = " ".join(" ".join(part) for part in argv)
    assert "qdisc del" in flat
    assert f"{net.name}eg" in flat
    assert "link delete" in flat


def test_egress_host_and_session_hosts(tmp_path: Path) -> None:
    assert egress_host("https://api.openai.com/v1") == "api.openai.com"
    assert egress_host("mcp.tavily.com") == "mcp.tavily.com"
    settings = _settings(tmp_path).model_copy(
        update={
            "model_base_url": "https://api.openai.com/v1",
            "microvm_egress_hosts": "mcp.tavily.com",
        }
    )
    mcp = [
        McpHttpServer(
            server_label="search",
            server_url="https://mcp.example.com/mcp",
            headers={},
        )
    ]
    hosts = microvm_egress_hosts(settings, mcp)
    assert hosts == ["api.openai.com", "mcp.tavily.com", "mcp.example.com"]


def test_resolve_host_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apipi.pi.microvm.socket.getaddrinfo",
        lambda *_a, **_k: [
            (0, 0, 0, "", ("203.0.113.10", 0)),
            (0, 0, 0, "", ("203.0.113.10", 0)),
        ],
    )
    assert resolve_host_ips("api.example.com") == ["203.0.113.10"]


def test_guest_skill_dirs_rewrite_workspace_paths(tmp_path: Path) -> None:
    cwd = tmp_path / "session"
    inside = cwd / "pack" / "demo"
    inside.mkdir(parents=True)
    outside = tmp_path / "other" / "cap"
    outside.mkdir(parents=True)
    mapped, extras = guest_skill_dirs(str(cwd), [str(inside), str(outside)])
    assert mapped == [
        f"{GUEST_WORKSPACE}/pack/demo",
        f"{GUEST_WORKSPACE}/.apipi/skills/cap",
    ]
    assert extras == [(outside.resolve(), ".apipi/skills/cap")]


def test_workspace_image_packs_outside_skills(tmp_path: Path) -> None:
    cwd = tmp_path / "session"
    cwd.mkdir()
    outside = tmp_path / "cap-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("hello")
    dest = tmp_path / "workspace.tar"
    write_workspace_image(
        dest,
        cwd=str(cwd),
        env={},
        pi_args=["pi", "--skill", f"{GUEST_WORKSPACE}/.apipi/skills/cap-skill"],
        extra_dirs=[(outside, ".apipi/skills/cap-skill")],
    )
    with tarfile.open(dest, mode="r") as tar:
        names = tar.getnames()
    assert any(name.endswith("SKILL.md") for name in names)
    assert any(".apipi/skills/cap-skill" in name for name in names)


def test_guest_env_drops_host_path(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    env = guest_env(settings)
    assert "PATH" not in env
    assert "DATABASE_URL" not in env
    assert env["APIPI_PINNED_PI"]


class _Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _Process:
    def __init__(self) -> None:
        self.pid = 4242
        self.returncode = None
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        return self.returncode or 0


async def test_connect_vsock_handshake(tmp_path: Path) -> None:
    sock = tmp_path / "vsock.sock"
    got: dict[str, bytes] = {}

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        got["line"] = await reader.readline()
        writer.write(b"OK 1073741824\n")
        await writer.drain()

    server = await asyncio.start_unix_server(handler, path=str(sock))
    async with server:
        _reader, writer = await connect_vsock(sock, VSOCK_PORT, timeout=2)
        assert got["line"] == b"CONNECT 52\n"
        writer.close()
        await writer.wait_closed()


async def test_connect_vsock_timeout(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot start"):
        await connect_vsock(tmp_path / "missing.sock", VSOCK_PORT, timeout=0.05)


async def test_spawn_pi_microvm_uses_jailer_and_vsock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    cwd = tmp_path / "session"
    cwd.mkdir()
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.pi.microvm.setup_tap", lambda *_a, **_k: None)
    monkeypatch.setattr("apipi.pi.microvm.teardown_tap", lambda *_a, **_k: None)
    captured: dict[str, Any] = {}
    writer = _Writer()

    async def fake_exec(*args: str, **kwargs: Any) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    async def fake_connect(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        captured["vsock"] = True
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, writer

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("apipi.pi.microvm.connect_vsock", fake_connect)
    proc = await spawn_pi(_settings(tmp_path), cwd=str(cwd), tools=True)
    assert proc.process.pid == 4242
    args = captured["args"]
    assert args[0] == "/usr/bin/jailer"
    assert "/usr/bin/firecracker" in args
    assert "--no-api" in args
    assert captured["vsock"] is True
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] is asyncio.subprocess.DEVNULL
    await proc.send({"type": "prompt", "message": "hi"})
    assert b'"type": "prompt"' in writer.buf
    assert proc._stdin is writer


async def test_spawn_microvm_does_not_fallback_to_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: False)
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        await spawn_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_missing_firecracker_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", lambda _name: None)
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="firecracker"):
        await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_missing_ip_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.pi.microvm.shutil.which",
        lambda name: None if name == "ip" else _which_ok(name),
    )
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match=r"requires ip$"):
        await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_sets_up_tap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    taps: list[object] = []
    monkeypatch.setattr(
        "apipi.pi.microvm.setup_tap", lambda net, **_k: taps.append(net)
    )
    monkeypatch.setattr("apipi.pi.microvm.teardown_tap", lambda *_a, **_k: None)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    async def fake_connect(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _Writer()

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("apipi.pi.microvm.connect_vsock", fake_connect)
    await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert len(taps) == 1


def test_setup_tap_runs_ip_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        ran.append(list(argv))

    monkeypatch.setattr("apipi.pi.microvm._enable_forward", lambda: None)
    monkeypatch.setattr("apipi.pi.microvm._run", fake_run)
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    setup_tap(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        uid=1,
        gid=2,
        tc="/sbin/tc",
        allowed_ips=["203.0.113.10"],
        egress_mbit=25,
    )
    flat = " ".join(" ".join(part) for part in ran)
    assert "/sbin/ip tuntap add" in flat
    assert "MASQUERADE" in flat
    assert "127.0.0.1" not in flat
    assert "25mbit" in flat
    assert "203.0.113.10" in flat


async def test_spawn_microvm_stdio_stays_in_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.pi.microvm.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.pi.microvm.setup_tap", lambda *_a, **_k: None)
    monkeypatch.setattr("apipi.pi.microvm.teardown_tap", lambda *_a, **_k: None)
    captured: dict[str, Any] = {}

    async def fake_exec(*args: str, **_kwargs: Any) -> _Process:
        captured["args"] = args
        return _Process()

    async def fake_connect(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _Writer()

    monkeypatch.setattr("apipi.pi.microvm.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr("apipi.pi.microvm.connect_vsock", fake_connect)
    stdio = [
        McpStdioServer(
            server_label="local", command="npx", args=["-y", "mcp"], process=None
        )
    ]
    await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True, mcp_stdio=stdio)
    args = list(captured["args"])
    assert args[0] == "/usr/bin/jailer"
    assert "npx" not in args


def test_guest_starts_mcp_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[list[str]] = []

    def fake_popen(cmd: list[str], **_kwargs: Any) -> None:
        started.append(cmd)

    monkeypatch.setattr("apipi.pi.guest.subprocess.Popen", fake_popen)
    monkeypatch.setenv("APIPI_MCP_STDIO", "local")
    monkeypatch.setenv("APIPI_MCP_STDIO_0_COMMAND", "npx")
    monkeypatch.setenv("APIPI_MCP_STDIO_0_ARGS", "-y\x1fmcp")
    _start_mcp()
    assert started == [["npx", "-y", "mcp"]]


def test_guest_pi_args_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _pi_args() == ["pi", "--mode", "rpc", "--no-session"]


async def test_piproc_rpc_over_custom_streams() -> None:
    writer = _Writer()
    inner = _Process()
    proc = PiProc(cast(asyncio.subprocess.Process, inner), stdin=cast(Any, writer))
    await proc.send({"type": "abort"})
    assert json.loads(writer.buf.decode().strip()) == {"type": "abort"}
