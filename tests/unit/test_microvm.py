import asyncio
import errno
import ipaddress
import json
import logging
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from apipi.config import (
    BUILTIN_RUN_MODES,
    ConfigError,
    Settings,
    require_run_mode,
)
from apipi.env.setup import NetworkPolicy, write_network_policy
from apipi.mcp.http import McpConnectError, McpHttpServer
from apipi.worker.egress import EgressHooks
from apipi.worker.egress.dns import PLACEHOLDER_IP
from apipi.worker.pi.artifacts import unpack_workspace_tar
from apipi.worker.pi.guest import (
    RNDADDENTROPY,
    _pi_args,
    _seed_rng,
    workspace_tar_bytes,
)
from apipi.worker.pi.guest import main as guest_main
from apipi.worker.pi.microvm import (
    BOOT_ARGS,
    EGRESS_CA_REL,
    GUEST_DNS,
    GUEST_WORKSPACE,
    INSTALL_HINT,
    SHELL_SUDO_MARK,
    VSOCK_PORT,
    StartedMicrovm,
    TapPorts,
    _console_level,
    _disable_ipv6,
    _enable_forward,
    _run,
    connect_vsock,
    egress_host,
    env_file,
    guest_env,
    guest_skill_dirs,
    jailer_argv,
    log_sandbox_boot_failed,
    microvm_binaries,
    microvm_config,
    microvm_egress_hosts,
    microvm_images,
    microvm_net_binaries,
    microvm_shell_needs_sudo,
    microvm_shell_sudo_argv,
    require_microvm,
    run_microvm_shell,
    setup_tap,
    spawn_microvm_pi,
    start_microvm,
    tap_net,
    tap_setup_argv,
    tap_teardown_argv,
    write_workspace_image,
)
from apipi.worker.pi.proc import PiProc, spawn_pi

FAKE_CA = b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n"


class _FakeGateway:
    def __init__(self, mode: str, kwargs: dict[str, Any]) -> None:
        self.port = 40001
        self.dns_ports = (40002, 40003) if mode == "restricted" else None
        self.ca_pem = FAKE_CA
        self.kwargs = kwargs
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def gateways(monkeypatch: pytest.MonkeyPatch) -> list[_FakeGateway]:
    started: list[_FakeGateway] = []

    async def fake_start(_settings: Settings, **kwargs: Any) -> _FakeGateway:
        gateway = _FakeGateway(kwargs["mode"], kwargs)
        started.append(gateway)
        return gateway

    monkeypatch.setattr("apipi.worker.pi.microvm.start_gateway", fake_start)
    return started


def _settings(
    tmp_path: Path,
    *,
    run_mode: str = "microvm",
    kernel: str | None = None,
    rootfs: str | None = None,
    sandbox_default_image: str = "default",
    sandbox_default_size: Literal["S", "M", "L"] = "S",
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        pi_command="pi",
        microvm_kernel=kernel or str(tmp_path / "vmlinux"),
        microvm_rootfs=rootfs or str(tmp_path / "rootfs.ext4"),
        sandbox_default_image=sandbox_default_image,
        sandbox_default_size=sandbox_default_size,
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
    assert "microvm" in BUILTIN_RUN_MODES


def test_require_microvm_missing_kvm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: False)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_microvm(_settings(tmp_path))
    with pytest.raises(ConfigError, match="/dev/kvm"):
        require_run_mode("microvm")


def test_require_microvm_missing_firecracker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "firecracker" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="firecracker"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_jailer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "jailer" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="jailer"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_kernel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.write_bytes(b"r")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_KERNEL"):
        require_microvm(
            _settings(tmp_path, kernel=str(tmp_path / "missing"), rootfs=str(rootfs))
        )


def test_require_microvm_missing_rootfs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"k")
    with pytest.raises(ConfigError, match="APIPI_MICROVM_ROOTFS"):
        require_microvm(
            _settings(tmp_path, kernel=str(kernel), rootfs=str(tmp_path / "missing"))
        )


def test_microvm_images_default_uses_dev_override(tmp_path: Path) -> None:
    kernel, rootfs = _images(tmp_path)
    resolved = microvm_images(_settings(tmp_path))
    assert resolved == (str(kernel), str(rootfs))


def test_microvm_images_explicit_image_uses_dev_override(tmp_path: Path) -> None:
    kernel, rootfs = _images(tmp_path)
    resolved = microvm_images(
        _settings(tmp_path),
        image="browser",
    )
    assert resolved == (str(kernel), str(rootfs))


def test_microvm_images_store_missing_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        images_dir=str(images),
        sandbox_default_image="default",
    )
    with pytest.raises(ConfigError, match="APIPI_MICROVM_KERNEL"):
        microvm_images(settings)


def test_microvm_images_dev_override_wins(tmp_path: Path) -> None:
    kernel, rootfs = _images(tmp_path)
    assert microvm_images(_settings(tmp_path)) == (str(kernel), str(rootfs))


def test_microvm_images_missing_explains_pull(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        images_dir=str(images),
        sandbox_default_image="default",
    )
    with pytest.raises(ConfigError, match="APIPI_MICROVM_KERNEL") as exc:
        microvm_images(settings)
    assert INSTALL_HINT in str(exc.value)


def test_microvm_binaries_uses_install_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", lambda _name: None)
    prefix = tmp_path / "apipi" / "firecracker"
    prefix.mkdir(parents=True)
    firecracker = prefix / "firecracker"
    jailer = prefix / "jailer"
    firecracker.write_text("")
    jailer.write_text("")
    firecracker.chmod(0o755)
    jailer.chmod(0o755)
    assert microvm_binaries() == (str(firecracker), str(jailer))


def test_microvm_shell_sudo_argv_preserves_path_and_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    argv = microvm_shell_sudo_argv(
        ["--image", "browser"],
        executable="/venv/bin/python",
        home="/home/agent",
        path="/home/agent/.local/bin:/usr/bin",
    )
    assert argv[:3] == ["sudo", "-E", "env"]
    assert "PATH=/home/agent/.local/bin:/usr/bin" in argv
    assert "HOME=/home/agent" in argv
    assert f"{SHELL_SUDO_MARK}=1" in argv
    assert argv[-7:] == [
        "/venv/bin/python",
        "-m",
        "apipi",
        "microvm",
        "shell",
        "--image",
        "browser",
    ]


def test_microvm_shell_needs_sudo_skips_when_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.os.geteuid", lambda: 0)
    assert microvm_shell_needs_sudo() is False


def test_microvm_shell_needs_sudo_skips_after_reexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.os.geteuid", lambda: 1000)
    monkeypatch.setenv(SHELL_SUDO_MARK, "1")
    assert microvm_shell_needs_sudo() is False


def test_require_microvm_missing_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "ip" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="requires ip"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_iptables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "iptables" else _which_ok(name),
    )
    with pytest.raises(ConfigError, match="requires iptables"):
        require_microvm(_settings(tmp_path))


def test_require_microvm_missing_tc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
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
    assert config["entropy"] == {}


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
        assert ".pi/agent/extensions/apipi-mcp.ts" in names
        assert ".pi/agent/extensions/apipi.ts" in names
        env = tar.extractfile(".apipi/env")
        assert env is not None
        text = env.read().decode()
        net_f = tar.extractfile(".apipi/net")
        assert net_f is not None
        net_text = net_f.read().decode()
        rnd = tar.extractfile(".apipi/random")
        assert rnd is not None
        assert len(rnd.read()) == 256
        assert ".apipi/playwright.json" not in names
    assert "OPENAI_API_KEY" in text
    assert "DATABASE_URL" not in text
    assert net.guest_ip in net_text
    assert net.host_ip in net_text
    assert "127.0.0.1" not in net_text
    assert ".apipi/shell" not in names


def test_mcp_console_is_info() -> None:
    assert _console_level("mcp: tools/call failed after 120000ms") == logging.INFO
    assert _console_level("pi ready") == logging.DEBUG


def test_seed_rng_credits_host_random(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret = tmp_path / ".apipi"
    secret.mkdir()
    secret.joinpath("random").write_bytes(b"x" * 256)
    monkeypatch.setenv("HOME", str(tmp_path))
    called: list[tuple[int, bytes]] = []

    def fake_ioctl(_fd: int, request: int, arg: bytes) -> int:
        called.append((request, arg))
        return 0

    monkeypatch.setattr("apipi.worker.pi.guest.fcntl.ioctl", fake_ioctl)
    _seed_rng()
    assert called
    assert called[0][0] == RNDADDENTROPY
    assert b"x" * 256 in called[0][1]


def test_workspace_image_includes_setup_script(tmp_path: Path) -> None:
    cwd = tmp_path / "session"
    cwd.mkdir()
    apipi = cwd / ".apipi"
    apipi.mkdir()
    (apipi / "setup.sh").write_text("#!/bin/sh\necho ok\n")
    dest = tmp_path / "workspace.tar"
    write_workspace_image(
        dest,
        cwd=str(cwd),
        env={},
        pi_args=["pi", "--mode", "rpc", "--no-session"],
    )
    with tarfile.open(dest, mode="r") as tar:
        names = tar.getnames()
        setup = next(name for name in names if name.endswith("setup.sh"))
        member = tar.extractfile(setup)
        assert member is not None
        assert b"echo ok" in member.read()


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


NET_ID = "551e7604-e35c-42b3-b825-416853441234"
PORTS = TapPorts(broker=40000, gateway=40001, dns_udp=40002, dns_tcp=40003)
BROKER_ONLY = TapPorts(broker=40000)


def _tap_argv(mode: str, ports: TapPorts = PORTS) -> list[list[str]]:
    return tap_setup_argv(
        tap_net(NET_ID),
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables="/sbin/ip6tables",
        uid=123,
        gid=100,
        ports=ports,
        tc="/sbin/tc",
        mode=cast(Any, mode),
    )


def _flat(argv: list[list[str]]) -> str:
    return "\n".join(" ".join(part) for part in argv)


def test_tap_setup_nat_without_host_loopback() -> None:
    net = tap_net(NET_ID)
    flat = _flat(_tap_argv("enabled"))
    assert net.name in flat
    assert "tuntap" in flat
    assert "MASQUERADE" in flat
    assert "127.0.0.1" not in flat
    assert "--map-host-loopback" not in flat
    assert "10.0.0.0/8" in flat
    assert "-j ACCEPT" in flat
    assert "50mbit" in flat


def test_tap_setup_enabled_sends_web_ports_to_gateway() -> None:
    net = tap_net(NET_ID)
    argv = _tap_argv("enabled")
    flat = _flat(argv)
    assert (
        f"-t nat -A {net.name}gw -p tcp -m multiport --dports 80,443,8443 "
        f"-j DNAT --to-destination {net.host_ip}:40001"
    ) in flat
    assert f"-t nat -A {net.name}gw -d {net.subnet} -j RETURN" in flat
    assert f"-t nat -I PREROUTING 1 -i {net.name}" in flat
    assert "--dport 53" not in flat
    assert f"-A {net.name}eg -p udp --dport 443 -j REJECT" in flat
    chain = [cmd for cmd in argv if len(cmd) > 3 and cmd[3] == f"{net.name}eg"]
    assert chain[-1] == ["/sbin/iptables", "-w", "-A", f"{net.name}eg", "-j", "ACCEPT"]
    accept_at = next(
        i for i, cmd in enumerate(argv) if net.subnet in cmd and "ACCEPT" in cmd
    )
    reject_at = next(i for i, cmd in enumerate(argv) if "172.16.0.0/12" in cmd)
    assert accept_at < reject_at


def test_tap_setup_restricted_rejects_other_ports_and_filters_dns() -> None:
    net = tap_net(NET_ID)
    argv = _tap_argv("restricted")
    flat = _flat(argv)
    assert f"--to-destination {net.host_ip}:40001" in flat
    assert (
        f"-t nat -A {net.name}gw -p udp --dport 53 -j DNAT "
        f"--to-destination {net.host_ip}:40002"
    ) in flat
    assert (
        f"-t nat -A {net.name}gw -p tcp --dport 53 -j DNAT "
        f"--to-destination {net.host_ip}:40003"
    ) in flat
    chain = [cmd for cmd in argv if len(cmd) > 3 and cmd[3] == f"{net.name}eg"]
    assert chain[-1][-4:] == [
        "-j",
        "REJECT",
        "--reject-with",
        "icmp-port-unreachable",
    ]
    assert chain[-1][4:6] == ["-j", "REJECT"]
    assert not any(cmd[-2:] == ["-j", "ACCEPT"] and len(cmd) == 6 for cmd in chain)
    for dns in GUEST_DNS:
        assert dns not in flat
    assert "203.0.113.10" not in flat


def test_dns_placeholder_is_sent_to_the_gateway() -> None:
    net = tap_net(NET_ID)
    assert ipaddress.ip_address(PLACEHOLDER_IP) not in ipaddress.ip_network(net.subnet)
    argv = _tap_argv("restricted")
    nat = [cmd for cmd in argv if "nat" in cmd and f"{net.name}gw" in cmd]
    web = [cmd for cmd in nat if "80,443,8443" in cmd]
    assert len(web) == 1
    assert "-d" not in web[0]
    returns = [cmd for cmd in nat if "RETURN" in cmd]
    assert [cmd[cmd.index("-d") + 1] for cmd in returns] == [net.subnet]
    assert nat.index(web[0]) > nat.index(returns[0])


def test_tap_setup_disabled_blocks_without_gateway() -> None:
    net = tap_net(NET_ID)
    argv = _tap_argv("disabled", BROKER_ONLY)
    flat = _flat(argv)
    assert "DNAT" not in flat
    assert f"{net.name}gw" not in flat
    assert "--dport 53" not in flat
    for dns in GUEST_DNS:
        assert dns not in flat
    chain = [cmd for cmd in argv if len(cmd) > 3 and cmd[3] == f"{net.name}eg"]
    assert chain[-1][4:6] == ["-j", "REJECT"]
    inbound = [cmd for cmd in argv if len(cmd) > 3 and cmd[3] == f"{net.name}in"]
    accepts = [cmd for cmd in inbound if "--dport" in cmd]
    assert [cmd[cmd.index("--dport") + 1] for cmd in accepts] == ["40000"]


def test_tap_setup_filters_guest_to_host_input() -> None:
    net = tap_net(NET_ID)
    argv = _tap_argv("restricted")
    chain = f"{net.name}in"
    inbound = [cmd for cmd in argv if len(cmd) > 3 and cmd[3] == chain]
    assert inbound[0] == ["/sbin/iptables", "-w", "-N", chain]
    assert "RELATED,ESTABLISHED" in inbound[1]
    allowed = [
        (cmd[cmd.index("-p") + 1], cmd[cmd.index("--dport") + 1])
        for cmd in inbound
        if "--dport" in cmd
    ]
    assert allowed == [
        ("tcp", "40000"),
        ("tcp", "40001"),
        ("udp", "40002"),
        ("tcp", "40003"),
    ]
    for cmd in inbound:
        if "--dport" in cmd:
            assert cmd[cmd.index("-d") + 1] == net.host_ip
    assert inbound[-1][4:6] == ["-j", "REJECT"]
    jump = f"-I INPUT 1 -i {net.name} -m comment --comment apipi-{net.name} -j {chain}"
    assert jump in _flat(argv)
    enabled = _tap_argv("enabled", TapPorts(broker=40000, gateway=40001))
    enabled_in = [cmd for cmd in enabled if len(cmd) > 3 and cmd[3] == chain]
    assert [
        cmd[cmd.index("--dport") + 1] for cmd in enabled_in if "--dport" in cmd
    ] == [
        "40000",
        "40001",
    ]


def test_tap_setup_needs_gateway_unless_disabled() -> None:
    with pytest.raises(ValueError, match="gateway"):
        _tap_argv("enabled", BROKER_ONLY)
    with pytest.raises(ValueError, match="DNS"):
        _tap_argv("restricted", TapPorts(broker=40000, gateway=40001))


def _table(cmd: list[str]) -> str:
    return cmd[cmd.index("-t") + 1] if "-t" in cmd else "filter"


@pytest.mark.parametrize("mode", ["enabled", "restricted", "disabled"])
def test_tap_teardown_removes_every_rule(mode: str) -> None:
    net = tap_net(NET_ID)
    setup = _tap_argv(mode, BROKER_ONLY if mode == "disabled" else PORTS)
    teardown = tap_teardown_argv(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables="/sbin/ip6tables",
        tc="/sbin/tc",
        mode=cast(Any, mode),
    )
    builtin = {"FORWARD", "INPUT", "POSTROUTING", "PREROUTING"}
    for cmd in setup:
        if Path(cmd[0]).name not in ("iptables", "ip6tables"):
            continue
        table = _table(cmd)
        for flag in ("-A", "-I"):
            if flag in cmd and cmd[cmd.index(flag) + 1] in builtin:
                at = cmd.index(flag)
                rest = cmd[at + 2 :]
                if flag == "-I" and rest and rest[0].isdigit():
                    rest = rest[1:]
                expected = [*cmd[:at], "-D", cmd[at + 1], *rest]
                assert expected in teardown, expected
        if "-N" in cmd:
            chain = cmd[cmd.index("-N") + 1]
            flushes = [c for c in teardown if "-F" in c and chain in c]
            drops = [c for c in teardown if "-X" in c and chain in c]
            assert flushes and drops
            assert {_table(c) for c in [*flushes, *drops]} == {table}
            assert teardown.index(flushes[0]) < teardown.index(drops[0])
    assert ["/sbin/ip", "link", "delete", "dev", net.name] == teardown[-1]
    teardown_flat = _flat(teardown)
    assert "qdisc del" in teardown_flat
    if mode == "disabled":
        assert f"{net.name}gw" not in teardown_flat


def test_tap_setup_closes_ipv6() -> None:
    net = tap_net(NET_ID)
    for mode in ("enabled", "restricted", "disabled"):
        argv = _tap_argv(mode, BROKER_ONLY if mode == "disabled" else PORTS)
        drops = [cmd for cmd in argv if cmd[0] == "/sbin/ip6tables"]
        assert drops == [
            [
                "/sbin/ip6tables",
                "-w",
                "-I",
                builtin,
                "1",
                "-i",
                net.name,
                "-m",
                "comment",
                "--comment",
                f"apipi-{net.name}",
                "-j",
                "DROP",
            ]
            for builtin in ("INPUT", "FORWARD")
        ]
        up = argv.index(["/sbin/ip", "link", "set", "dev", net.name, "up"])
        assert all(argv.index(cmd) < up for cmd in drops)
    without = tap_setup_argv(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables=None,
        uid=1,
        gid=2,
        ports=PORTS,
    )
    assert not any("ip6tables" in cmd[0] for cmd in without)


def test_setup_tap_disables_ipv6_before_link_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    net = tap_net(NET_ID)
    ran: list[list[str]] = []
    sysctl = tmp_path / "ipv6"
    conf = sysctl / "conf" / net.name
    conf.mkdir(parents=True)

    def fake_run(argv: list[str]) -> None:
        if argv[1:3] == ["tuntap", "add"]:
            (conf / "disable_ipv6").write_text("0")
        ran.append([*argv, (conf / "disable_ipv6").read_text()])

    monkeypatch.setattr("apipi.worker.pi.microvm._enable_forward", lambda: None)
    monkeypatch.setattr("apipi.worker.pi.microvm._run", fake_run)
    monkeypatch.setattr("apipi.worker.pi.microvm.IPV6_SYS", sysctl)
    setup_tap(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables="/sbin/ip6tables",
        uid=1,
        gid=2,
        ports=PORTS,
        mode="restricted",
    )
    assert ran[0][1:3] == ["tuntap", "add"]
    assert ran[0][-1] == "0"
    assert all(cmd[-1] == "1" for cmd in ran[1:])
    assert any(cmd[0] == "/sbin/ip6tables" for cmd in ran)
    ran.clear()
    monkeypatch.setattr("apipi.worker.pi.microvm.IPV6_SYS", tmp_path / "missing")
    setup_tap(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables="/sbin/ip6tables",
        uid=1,
        gid=2,
        ports=PORTS,
        mode="restricted",
    )
    assert ran and not any(cmd[0] == "/sbin/ip6tables" for cmd in ran)


def test_disable_ipv6_failure_names_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.IPV6_SYS", tmp_path)
    with pytest.raises(ConfigError, match="disable IPv6 on the TAP device"):
        _disable_ipv6(tap_net(NET_ID))


def test_microvm_needs_ip6tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "ip6tables" else f"/sbin/{name}",
    )
    with pytest.raises(ConfigError, match="microvm requires ip6tables"):
        microvm_net_binaries()


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
    with_packages = microvm_egress_hosts(
        settings, mcp, extra_hosts=["pypi.org", "registry.npmjs.org"]
    )
    assert with_packages[-2:] == ["pypi.org", "registry.npmjs.org"]


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
    env = guest_env(settings, extra_env={"REPORT": "yes"})
    assert env["REPORT"] == "yes"
    assert "PATH" not in env


def test_guest_env_drops_worker_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APIPI_WORKER_TOKEN_FILE", "/run/apipi/worker.token")
    monkeypatch.setenv("APIPI_DATABASE_URL", "postgresql://apipi:db-secret@db/apipi")
    monkeypatch.setenv("APIPI_DB_PASSWORD", "db-secret")
    monkeypatch.setenv("APIPI_DB_USER", "apipi")
    monkeypatch.setenv("APIPI_API_URL", "http://100.68.58.157:8080")
    monkeypatch.setenv("OPENAI_API_KEY_OVERWRITE", "operator-key")
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://172.16.187.113:9/upstream/v1")
    vault = "aa" * 32
    monkeypatch.setenv("APIPI_VAULT_MASTER_KEY", vault)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:db-secret@db/apipi")
    monkeypatch.setenv("APIPI_MCP_0_AUTHORIZATION", "Bearer host-secret")

    class _Broker:
        openai_base_url = "http://172.16.0.1:1/tok/v1"

        def mcp_url(self, route_id: str) -> str:
            return f"http://172.16.0.1:1/tok/mcp/{route_id}"

    mcp = [
        McpHttpServer(
            server_label="tavily",
            server_url="https://mcp.example/mcp",
            headers={"Authorization": "Bearer mcp-secret"},
        )
    ]
    env = guest_env(
        _settings(tmp_path),
        mcp,
        api_key="real-key",
        broker=_Broker(),
        extra_env={"REPORT": "yes", "OPENAI_API_KEY": "from-session"},
    )
    packed = "\n".join(f"{key}={value}" for key, value in env.items())
    for secret in (
        "/run/apipi/worker.token",
        "db-secret",
        "operator-key",
        "process-key",
        vault,
        "real-key",
        "mcp-secret",
        "host-secret",
        "from-session",
        "upstream",
        "100.68.58.157",
    ):
        assert secret not in packed
    assert env["OPENAI_API_KEY"] == "apipi"
    assert env["OPENAI_BASE_URL"] == "http://172.16.0.1:1/tok/v1"
    assert env["APIPI_MCP_0_URL"] == "http://172.16.0.1:1/tok/mcp/0"
    assert "APIPI_MCP_0_AUTHORIZATION" not in env
    assert env["REPORT"] == "yes"
    assert "APIPI_WORKER_TOKEN_FILE" not in env
    assert "APIPI_API_URL" not in env
    assert "OPENAI_API_KEY_OVERWRITE" not in env
    forced = env_file(
        {
            **env,
            "APIPI_DB_PASSWORD": "db-secret",
            "OPENAI_API_KEY_OVERWRITE": "operator-key",
            "APIPI_WORKER_TOKEN_FILE": "/run/apipi/worker.token",
        }
    )
    assert "db-secret" not in forced
    assert "operator-key" not in forced
    assert "/run/apipi/worker.token" not in forced
    assert "OPENAI_API_KEY=apipi" in forced


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
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.worker.pi.microvm.setup_tap", lambda *_a, **_k: None)
    monkeypatch.setattr("apipi.worker.pi.microvm.teardown_tap", lambda *_a, **_k: None)
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

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("apipi.worker.pi.microvm.connect_vsock", fake_connect)
    proc = await spawn_pi(_settings(tmp_path), cwd=str(cwd), tools=True)
    assert proc.process.pid == 4242
    args = captured["args"]
    assert args[0] == "/usr/bin/jailer"
    assert "/usr/bin/firecracker" in args
    assert "--no-api" in args
    assert captured["vsock"] is True
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is asyncio.subprocess.PIPE
    assert captured["kwargs"]["stderr"] is asyncio.subprocess.PIPE
    assert captured["kwargs"].get("start_new_session") in (None, False)
    assert proc.process_group is False
    assert proc.push_files is not None
    await proc.send({"type": "prompt", "message": "hi"})
    assert b'"type": "prompt"' in writer.buf
    assert proc._stdin is writer


async def test_spawn_microvm_does_not_fallback_to_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: False)
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="/dev/kvm"):
        await spawn_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_missing_firecracker_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", lambda _name: None)
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="firecracker"):
        await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_missing_ip_does_not_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.shutil.which",
        lambda name: None if name == "ip" else _which_ok(name),
    )
    called = False

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        nonlocal called
        called = True
        return _Process()

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(ConfigError, match="requires ip"):
        await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert called is False


async def test_spawn_microvm_sets_up_tap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    taps: list[object] = []
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.setup_tap", lambda net, **_k: taps.append(net)
    )
    monkeypatch.setattr("apipi.worker.pi.microvm.teardown_tap", lambda *_a, **_k: None)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    async def fake_connect(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _Writer()

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("apipi.worker.pi.microvm.connect_vsock", fake_connect)
    await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert len(taps) == 1


def test_setup_tap_runs_ip_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        ran.append(list(argv))

    monkeypatch.setattr("apipi.worker.pi.microvm._enable_forward", lambda: None)
    monkeypatch.setattr("apipi.worker.pi.microvm._run", fake_run)
    net = tap_net("551e7604-e35c-42b3-b825-416853441234")
    monkeypatch.setattr("apipi.worker.pi.microvm._disable_ipv6", lambda _net: None)
    setup_tap(
        net,
        ip="/sbin/ip",
        iptables="/sbin/iptables",
        ip6tables="/sbin/ip6tables",
        uid=1,
        gid=2,
        tc="/sbin/tc",
        ports=TapPorts(broker=40000, gateway=40001),
        egress_mbit=25,
    )
    flat = " ".join(" ".join(part) for part in ran)
    assert "/sbin/ip tuntap add" in flat
    assert "MASQUERADE" in flat
    assert "127.0.0.1" not in flat
    assert "25mbit" in flat
    assert "10.0.0.0/8" in flat
    assert "REJECT" in flat


def test_run_tap_permission_names_rights(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1,
            argv,
            stderr=b"ioctl(TUNSETIFF): Operation not permitted\n",
        )

    monkeypatch.setattr("apipi.worker.pi.microvm.subprocess.run", fake_run)
    with pytest.raises(ConfigError, match="TAP device") as err:
        _run(["/sbin/ip", "tuntap", "add", "dev", "apipix", "mode", "tap"])
    text = str(err.value)
    assert "Operation not permitted" in text
    assert "CAP_NET_ADMIN" in text


def test_run_iptables_permission_names_rights(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, argv, stderr=b"Permission denied\n")

    monkeypatch.setattr("apipi.worker.pi.microvm.subprocess.run", fake_run)
    with pytest.raises(ConfigError, match="iptables") as err:
        _run(["/sbin/iptables", "-w", "-A", "FORWARD"])
    text = str(err.value)
    assert "Permission denied" in text
    assert "CAP_NET_ADMIN" in text


def test_run_other_failure_includes_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(
            1, argv, stderr=b'Cannot find device "apipix"\n'
        )

    monkeypatch.setattr("apipi.worker.pi.microvm.subprocess.run", fake_run)
    with pytest.raises(ConfigError, match="Cannot find device") as err:
        _run(["/sbin/ip", "link", "set", "apipix", "up"])
    assert "CAP_NET_ADMIN" not in str(err.value)


def test_enable_forward_permission_names_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Denied:
        def read_text(self, *_a: object, **_k: object) -> str:
            return "0\n"

        def write_text(self, *_a: object, **_k: object) -> None:
            raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("apipi.worker.pi.microvm.Path", lambda *_a, **_k: Denied())
    with pytest.raises(ConfigError, match="ip_forward") as err:
        _enable_forward()
    text = str(err.value)
    assert "Permission denied" in text
    assert "CAP_NET_ADMIN" in text


async def test_start_microvm_jailer_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    with pytest.raises(ConfigError, match="jailer") as err:
        await start_microvm(_settings(tmp_path), cwd=None, tools=True)
    text = str(err.value)
    assert "Permission denied" in text
    assert "root" in text


def test_guest_shell_execs_sh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "workspace"
    home.mkdir()
    (home / ".apipi").mkdir()
    (home / ".apipi" / "shell").write_bytes(b"")
    monkeypatch.setenv("HOME", str(home))
    called: list[tuple[str, list[str]]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        called.append((file, list(args)))
        raise SystemExit(0)

    monkeypatch.setattr("apipi.worker.pi.guest.os.execvp", fake_execvp)
    monkeypatch.setattr("apipi.worker.pi.guest.os.chdir", lambda _path: None)
    with pytest.raises(SystemExit):
        guest_main([])
    assert called == [("sh", ["sh", "-i"])]


def _microvm_spawn_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    monkeypatch.setattr("apipi.worker.pi.microvm.setup_tap", lambda *_a, **_k: None)
    monkeypatch.setattr("apipi.worker.pi.microvm.teardown_tap", lambda *_a, **_k: None)


async def test_start_microvm_shell_inherits_stdio_and_skips_vsock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)
    packed: dict[str, Any] = {}
    captured: dict[str, Any] = {}

    def fake_write(dest: Path, **kwargs: Any) -> None:
        packed.update(kwargs)
        packed["dest"] = dest
        write_workspace_image(dest, **kwargs)

    async def fake_exec(*args: str, **kwargs: Any) -> _Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Process()

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shell boot must not wait on vsock")

    monkeypatch.setattr("apipi.worker.pi.microvm.write_workspace_image", fake_write)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("apipi.worker.pi.microvm.connect_vsock", boom)
    started = await start_microvm(
        _settings(tmp_path),
        cwd=None,
        tools=True,
        shell=True,
        inherit_stdio=True,
    )
    assert packed["shell"] is True
    assert captured["kwargs"]["stdin"] is None
    assert captured["kwargs"]["stdout"] is None
    assert captured["kwargs"]["stderr"] is None
    assert captured["args"][0] == "/usr/bin/jailer"
    assert "--no-api" in captured["args"]
    assert started.process.pid == 4242


async def test_spawn_microvm_pi_does_not_set_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)
    packed: dict[str, Any] = {}

    def fake_write(dest: Path, **kwargs: Any) -> None:
        packed.update(kwargs)
        write_workspace_image(dest, **kwargs)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    async def fake_connect(*_args: object, **_kwargs: object) -> tuple[object, _Writer]:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        return reader, _Writer()

    monkeypatch.setattr("apipi.worker.pi.microvm.write_workspace_image", fake_write)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    monkeypatch.setattr("apipi.worker.pi.microvm.connect_vsock", fake_connect)
    await spawn_microvm_pi(_settings(tmp_path), cwd=None, tools=True)
    assert packed.get("shell") is False


async def test_run_microvm_shell_waits_and_cleans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cleaned: list[int] = []
    captured: dict[str, Any] = {}

    class _Done(_Process):
        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_start(_settings: Settings, **kwargs: Any) -> StartedMicrovm:
        captured.update(kwargs)
        return StartedMicrovm(
            cast(asyncio.subprocess.Process, _Done()),
            tmp_path,
            lambda: cleaned.append(1),
        )

    monkeypatch.setattr("apipi.worker.pi.microvm.start_microvm", fake_start)
    assert await run_microvm_shell(_settings(tmp_path), cwd=str(tmp_path)) == 0
    assert captured["shell"] is True
    assert captured["inherit_stdio"] is True
    assert captured["tools"] is True
    assert captured["cwd"] == str(tmp_path.resolve())
    assert cleaned == [1]


async def test_run_microvm_shell_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--workspace must be a directory"):
        await run_microvm_shell(_settings(tmp_path), cwd=str(tmp_path / "missing"))


def test_guest_pi_args_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _pi_args() == ["pi", "--mode", "rpc", "--no-session"]


async def test_piproc_rpc_over_custom_streams() -> None:
    writer = _Writer()
    inner = _Process()
    proc = PiProc(cast(asyncio.subprocess.Process, inner), stdin=cast(Any, writer))
    await proc.send({"type": "abort"})
    assert json.loads(writer.buf.decode().strip()) == {"type": "abort"}


def test_sandbox_boot_failed_is_structured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi.microvm")
    log_sandbox_boot_failed(ConfigError("microvm cannot start"), vm_id="vm-1")
    records = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "sandbox.boot.failed"
    ]
    assert records
    last = records[-1]
    assert last.levelno == logging.ERROR
    assert last.__dict__["error_code"] == "sandbox_boot_failed"
    assert last.__dict__["vm_id"] == "vm-1"


async def test_start_microvm_runs_egress_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    setups: list[dict[str, Any]] = []
    teardowns: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.setup_tap", lambda net, **kw: setups.append(kw)
    )
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.teardown_tap", lambda net, **kw: teardowns.append(kw)
    )
    packed: dict[str, bytes] = {}

    def fake_write(dest: Path, **kwargs: Any) -> None:
        write_workspace_image(dest, **kwargs)
        with tarfile.open(dest, mode="r") as tar:
            member = tar.extractfile(EGRESS_CA_REL)
            assert member is not None
            packed["ca"] = member.read()

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    monkeypatch.setattr("apipi.worker.pi.microvm.write_workspace_image", fake_write)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    cwd = tmp_path / "session"
    cwd.mkdir()
    write_network_policy(
        cwd, NetworkPolicy(access="restricted", allowed_domains=("api.example.com",))
    )
    hooks = EgressHooks()
    started = await start_microvm(
        _settings(tmp_path),
        cwd=str(cwd),
        tools=True,
        session_id="sess_1",
        intercept_hosts=("api.example.com",),
        egress_hooks=hooks,
    )
    try:
        assert len(gateways) == 1
        gateway = gateways[0]
        assert started.egress is cast(Any, gateway)
        kwargs = gateway.kwargs
        assert kwargs["mode"] == "restricted"
        assert kwargs["allowed_hosts"] == ("api.example.com",)
        assert kwargs["host"] == tap_net(started.chroot_dir.parent.name).host_ip
        assert kwargs["session_id"] == "sess_1"
        assert kwargs["intercept_hosts"] == ("api.example.com",)
        assert kwargs["hooks"] is hooks
        assert kwargs["dns_upstreams"] == tuple((dns, 53) for dns in GUEST_DNS)
        assert setups[0]["mode"] == "restricted"
        assert setups[0]["ip6tables"] == "/usr/bin/ip6tables"
        assert started.broker is not None
        assert setups[0]["ports"] == TapPorts(started.broker.port, 40001, 40002, 40003)
        assert packed["ca"] == FAKE_CA
    finally:
        if started.broker is not None:
            await started.broker.stop()
        started.cleanup()
    assert gateway.closed is True
    assert teardowns[0]["mode"] == "restricted"
    assert teardowns[0]["ip6tables"] == "/usr/bin/ip6tables"


async def test_start_microvm_enabled_gateway_has_no_dns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)
    setups: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.setup_tap", lambda net, **kw: setups.append(kw)
    )

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    started = await start_microvm(_settings(tmp_path), cwd=None, tools=True)
    try:
        assert gateways[0].kwargs["mode"] == "enabled"
        assert gateways[0].kwargs["intercept_hosts"] == ()
        assert started.broker is not None
        assert setups[0]["ports"] == TapPorts(started.broker.port, 40001)
    finally:
        if started.broker is not None:
            await started.broker.stop()
        started.cleanup()


async def test_start_microvm_disabled_runs_no_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)
    setups: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.setup_tap", lambda net, **kw: setups.append(kw)
    )
    packed: dict[str, Any] = {}

    def fake_write(dest: Path, **kwargs: Any) -> None:
        packed.update(kwargs)
        write_workspace_image(dest, **kwargs)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    monkeypatch.setattr("apipi.worker.pi.microvm.write_workspace_image", fake_write)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    cwd = tmp_path / "session"
    cwd.mkdir()
    write_network_policy(cwd, NetworkPolicy(access="disabled"))
    started = await start_microvm(_settings(tmp_path), cwd=str(cwd), tools=True)
    try:
        assert gateways == []
        assert started.egress is None
        assert started.broker is not None
        assert setups[0]["mode"] == "disabled"
        assert setups[0]["ports"] == TapPorts(started.broker.port)
        assert packed["egress_ca"] is None
    finally:
        if started.broker is not None:
            await started.broker.stop()
        started.cleanup()


async def test_start_microvm_boot_failure_closes_gateway(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    with pytest.raises(ConfigError):
        await start_microvm(_settings(tmp_path), cwd=None, tools=True)
    assert gateways[0].closed is True


def test_require_microvm_checks_upstream_ca(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _images(tmp_path)
    monkeypatch.setattr("apipi.worker.pi.microvm.kvm_available", lambda: True)
    monkeypatch.setattr("apipi.worker.pi.microvm.shutil.which", _which_ok)
    settings = _settings(tmp_path).model_copy(
        update={"microvm_egress_upstream_ca": str(tmp_path / "missing.pem")}
    )
    with pytest.raises(ConfigError, match="APIPI_MICROVM_EGRESS_UPSTREAM_CA"):
        require_microvm(settings)
    bad = tmp_path / "bad.pem"
    bad.write_text("not a certificate\n")
    settings = settings.model_copy(update={"microvm_egress_upstream_ca": str(bad)})
    with pytest.raises(ConfigError, match="not a valid PEM bundle"):
        require_microvm(settings)


def test_guest_sh_builds_ca_bundle() -> None:
    script = Path(__file__).resolve().parents[2] / "src/apipi/worker/pi/guest.sh"
    text = script.read_text()
    assert f'"$WS/{EGRESS_CA_REL}"' in text
    assert "/etc/ssl/certs/ca-certificates.crt" in text
    assert "/run/apipi" in text
    bundle_at = text.index('CA_BUNDLE="$CA_DIR/ca-bundle.pem"')
    env_at = text.index('. "$WS/.apipi/env"')
    pi_at = text.index("guest.py")
    for name in (
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "NODE_EXTRA_CA_CERTS",
    ):
        export_at = text.index(f'export {name}="$CA_BUNDLE"')
        assert bundle_at < env_at < export_at < pi_at


@pytest.mark.parametrize(
    "error", [McpConnectError("mcp blocked"), asyncio.CancelledError()]
)
async def test_start_microvm_closes_gateway_on_any_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
    error: BaseException,
) -> None:
    _microvm_spawn_ok(monkeypatch, tmp_path)
    torn: list[object] = []
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.teardown_tap", lambda net, **_k: torn.append(net)
    )

    async def failing_broker(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr("apipi.worker.pi.broker.start_broker", failing_broker)
    with pytest.raises(type(error)):
        await start_microvm(_settings(tmp_path), cwd=None, tools=True)
    assert gateways[0].closed is True
    assert len(torn) == 1


async def test_start_microvm_wires_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    gateways: list[_FakeGateway],
) -> None:
    from apipi.protocol import ContextEnvCredential

    _microvm_spawn_ok(monkeypatch, tmp_path)
    packed: dict[str, bytes] = {}
    names = (".apipi/env", ".apipi/git-credentials", ".apipi/git-credential")

    def fake_write(dest: Path, **kwargs: Any) -> None:
        write_workspace_image(dest, **kwargs)
        with tarfile.open(dest, mode="r") as tar:
            for name in names:
                member = tar.extractfile(name)
                assert member is not None
                packed[name] = member.read()

    async def fake_exec(*_args: str, **_kwargs: Any) -> _Process:
        return _Process()

    monkeypatch.setattr("apipi.worker.pi.microvm.write_workspace_image", fake_write)
    monkeypatch.setattr(
        "apipi.worker.pi.microvm.asyncio.create_subprocess_exec", fake_exec
    )
    cwd = tmp_path / "session"
    cwd.mkdir()
    write_network_policy(
        cwd, NetworkPolicy(access="restricted", allowed_domains=("pypi.org",))
    )
    hooks = EgressHooks()
    started = await start_microvm(
        _settings(tmp_path),
        cwd=str(cwd),
        tools=True,
        session_id="sess_1",
        intercept_hosts=("other.example.com",),
        egress_hooks=hooks,
        extra_env={"GITHUB_TOKEN": "user-value", "GIT_CONFIG_COUNT": "1"},
        env_credentials=[
            ContextEnvCredential(
                credential_id="cred_1",
                secret_name="GITHUB_TOKEN",
                secret_value="ghp-must-not-leak",
                allowed_hosts=["github.com", "api.github.com"],
            )
        ],
    )
    try:
        kwargs = gateways[0].kwargs
        assert kwargs["mode"] == "restricted"
        assert kwargs["allowed_hosts"] == ("pypi.org", "github.com", "api.github.com")
        assert kwargs["intercept_hosts"] == (
            "other.example.com",
            "github.com",
            "api.github.com",
        )
        assert kwargs["hooks"] is not hooks
        assert len(kwargs["hooks"].request) == 1
        assert len(kwargs["hooks"].response) == 1
        env_text = packed[".apipi/env"].decode()
        assert "GITHUB_TOKEN=apipi-secret-" in env_text
        assert "user-value" not in env_text
        assert "GIT_CONFIG_COUNT=9" in env_text
        assert "GIT_CONFIG_KEY_1=credential.https://github.com.helper" in env_text
        for blob in packed.values():
            assert b"ghp-must-not-leak" not in blob
        line = packed[".apipi/git-credentials"].decode().splitlines()[0]
        assert line.startswith("github.com\tx-access-token\tapipi-secret-")
        assert packed[".apipi/git-credential"].startswith(b"#!/bin/sh")
    finally:
        if started.broker is not None:
            await started.broker.stop()
        started.cleanup()


async def test_start_microvm_rejects_credentials_with_disabled_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apipi.protocol import ContextEnvCredential

    _microvm_spawn_ok(monkeypatch, tmp_path)
    cwd = tmp_path / "session"
    cwd.mkdir()
    write_network_policy(cwd, NetworkPolicy(access="disabled"))
    with pytest.raises(ConfigError, match="need network access"):
        await start_microvm(
            _settings(tmp_path),
            cwd=str(cwd),
            tools=True,
            env_credentials=[
                ContextEnvCredential(
                    credential_id="c",
                    secret_name="T",
                    secret_value="v",
                    allowed_hosts=["github.com"],
                )
            ],
        )
