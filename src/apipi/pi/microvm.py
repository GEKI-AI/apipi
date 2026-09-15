import asyncio
import contextlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

from apipi.config import ConfigError, Settings
from apipi.env.setup import workspace_egress_hosts
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.model_host import pi_agent_dir
from apipi.pi.proc import PiProc, pi_command_args, pi_env

VSOCK_PORT = 52
VSOCK_ARTIFACT_PORT = 53
VSOCK_WORKSPACE_PORT = 54
VSOCK_UDS = "vsock.sock"
MEM_MIB = 512
VCPU_COUNT = 1
CONNECT_TIMEOUT = 30.0
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/apipi-guest"
GUEST_WORKSPACE = "/workspace"
GUEST_DNS = ("1.1.1.1", "8.8.8.8")
TAP_NET_BASE = 0xAC100000
TAP_NET_SLOTS = 16384
SHELL_WARNING = (
    "Operator microVM shell. TAP egress allowlist still applies. "
    "Exit the shell or press Ctrl-C to stop the VM."
)


class TapNet(NamedTuple):
    name: str
    network: str
    host_ip: str
    guest_ip: str
    prefix: int
    mac: str

    @property
    def netmask(self) -> str:
        bits = (0xFFFFFFFF << (32 - self.prefix)) & 0xFFFFFFFF
        return _ipv4(bits)

    @property
    def subnet(self) -> str:
        return f"{self.network}/{self.prefix}"


class StartedMicrovm(NamedTuple):
    process: asyncio.subprocess.Process
    chroot_dir: Path
    cleanup: Callable[[], None]

    @property
    def vsock(self) -> Path:
        return self.chroot_dir / VSOCK_UDS


def kvm_available() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def microvm_binaries() -> tuple[str, str]:
    firecracker = shutil.which("firecracker")
    if firecracker is None:
        raise ConfigError("APIPI_RUN_MODE=microvm requires firecracker")
    jailer = shutil.which("jailer")
    if jailer is None:
        raise ConfigError("APIPI_RUN_MODE=microvm requires jailer")
    return firecracker, jailer


def microvm_net_binaries() -> tuple[str, str, str]:
    ip = shutil.which("ip")
    if ip is None:
        raise ConfigError("APIPI_RUN_MODE=microvm requires ip")
    iptables = shutil.which("iptables")
    if iptables is None:
        raise ConfigError("APIPI_RUN_MODE=microvm requires iptables")
    tc = shutil.which("tc")
    if tc is None:
        raise ConfigError("APIPI_RUN_MODE=microvm requires tc")
    return ip, iptables, tc


def microvm_image_name(settings: Settings | None = None) -> str:
    if settings is not None:
        return settings.microvm_image
    raw = os.environ.get("APIPI_MICROVM_IMAGE", "default")
    image = raw.strip() or "default"
    if image not in {"default", "browser"}:
        raise ConfigError("APIPI_MICROVM_IMAGE must be default or browser")
    return image


def microvm_images(settings: Settings | None = None) -> tuple[str, str]:
    if settings is not None:
        kernel = settings.microvm_kernel
        default_rootfs = settings.microvm_rootfs
        browser_rootfs = settings.microvm_rootfs_browser
    else:
        kernel = os.environ.get("APIPI_MICROVM_KERNEL")
        default_rootfs = os.environ.get("APIPI_MICROVM_ROOTFS")
        browser_rootfs = os.environ.get("APIPI_MICROVM_ROOTFS_BROWSER")
    if not kernel or not Path(kernel).is_file():
        raise ConfigError("APIPI_RUN_MODE=microvm requires APIPI_MICROVM_KERNEL")
    image = microvm_image_name(settings)
    if image == "browser":
        if not browser_rootfs or not Path(browser_rootfs).is_file():
            raise ConfigError(
                "APIPI_RUN_MODE=microvm requires APIPI_MICROVM_ROOTFS_BROWSER"
            )
        return kernel, browser_rootfs
    if not default_rootfs or not Path(default_rootfs).is_file():
        raise ConfigError("APIPI_RUN_MODE=microvm requires APIPI_MICROVM_ROOTFS")
    return kernel, default_rootfs


def require_microvm(settings: Settings | None = None) -> None:
    if not kvm_available():
        raise ConfigError("APIPI_RUN_MODE=microvm requires /dev/kvm")
    microvm_binaries()
    microvm_net_binaries()
    microvm_images(settings)


async def probe_microvm(settings: Settings) -> None:
    proc = await spawn_microvm_pi(settings, cwd=None, tools=False)
    try:
        if not proc.alive:
            raise ConfigError("APIPI_RUN_MODE=microvm cannot start")
    finally:
        await proc.terminate()


def guest_cid(vm_id: str) -> int:
    return uuid.UUID(vm_id).int % (2**32 - 3) + 3


def _ipv4(value: int) -> str:
    a = (value >> 24) & 255
    b = (value >> 16) & 255
    c = (value >> 8) & 255
    d = value & 255
    return f"{a}.{b}.{c}.{d}"


def egress_host(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname
    if host is None or host == "":
        return None
    return host


def microvm_egress_hosts(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    extra_hosts: list[str] | None = None,
) -> list[str]:
    found: list[str] = []
    if settings.model_base_url:
        host = egress_host(settings.model_base_url)
        if host is None:
            raise ConfigError("OPENAI_BASE_URL must include a host")
        found.append(host)
    for raw in settings.microvm_egress_hosts.split(","):
        item = raw.strip()
        if not item:
            continue
        host = egress_host(item)
        if host is None:
            raise ConfigError("APIPI_MICROVM_EGRESS_HOSTS must be hostnames")
        found.append(host)
    if mcp_http:
        for server in mcp_http:
            host = egress_host(server.server_url)
            if host is None:
                raise ConfigError("MCP server_url must include a host")
            found.append(host)
    if extra_hosts:
        for item in extra_hosts:
            host = egress_host(item)
            if host is None:
                raise ConfigError("APIPI_MICROVM_EGRESS_HOSTS must be hostnames")
            found.append(host)
    seen: set[str] = set()
    hosts: list[str] = []
    for host in found:
        key = host.lower()
        if key in seen:
            continue
        seen.add(key)
        hosts.append(host)
    return hosts


def resolve_host_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError as exc:
        raise ConfigError(f"APIPI_RUN_MODE=microvm cannot resolve {host}") from exc
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if not isinstance(ip, str) or ip in seen:
            continue
        seen.add(ip)
        ips.append(ip)
    if not ips:
        raise ConfigError(f"APIPI_RUN_MODE=microvm cannot resolve {host}")
    return ips


def allowed_egress_ips(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    extra_hosts: list[str] | None = None,
) -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()
    for host in microvm_egress_hosts(settings, mcp_http, extra_hosts=extra_hosts):
        for ip in resolve_host_ips(host):
            if ip in seen:
                continue
            seen.add(ip)
            ips.append(ip)
    return ips


def tap_net(vm_id: str) -> TapNet:
    ident = uuid.UUID(vm_id)
    base = TAP_NET_BASE + (ident.int % TAP_NET_SLOTS) * 4
    return TapNet(
        name=f"apipi{ident.hex[:8]}",
        network=_ipv4(base),
        host_ip=_ipv4(base + 1),
        guest_ip=_ipv4(base + 2),
        prefix=30,
        mac="02:FC:{:02x}:{:02x}:{:02x}:{:02x}".format(*ident.bytes[:4]),
    )


def boot_args(net: TapNet) -> str:
    dns0, dns1 = GUEST_DNS
    return (
        f"{BOOT_ARGS} ip={net.guest_ip}::{net.host_ip}:{net.netmask}"
        f"::eth0:off:{dns0}:{dns1}"
    )


def guest_env(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    *,
    api_key: str | None = None,
) -> dict[str, str]:
    env = pi_env(settings, mcp_http, mcp_stdio, api_key=api_key)
    env["PI_CODING_AGENT_DIR"] = f"{GUEST_WORKSPACE}/.pi/agent"
    return {
        key: value
        for key, value in env.items()
        if key.startswith("OPENAI_")
        or key.startswith("APIPI_")
        or key == "PI_CODING_AGENT_DIR"
    }


def env_file(env: dict[str, str]) -> str:
    lines = [
        f"{key}={shlex.quote(value)}"
        for key, value in env.items()
        if key != "DATABASE_URL"
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def net_file(net: TapNet) -> str:
    dns0, dns1 = GUEST_DNS
    values = {
        "GUEST_IP": net.guest_ip,
        "GUEST_PREFIX": str(net.prefix),
        "GUEST_GW": net.host_ip,
        "GUEST_MASK": net.netmask,
        "GUEST_DNS": dns0,
        "GUEST_DNS2": dns1,
    }
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())


def guest_skill_dirs(
    cwd: str | None,
    skill_dirs: list[str] | None,
) -> tuple[list[str] | None, list[tuple[Path, str]]]:
    extras: list[tuple[Path, str]] = []
    if skill_dirs is None:
        return None, extras
    mapped: list[str] = []
    root = Path(cwd).resolve() if cwd else None
    used: set[str] = set()
    for index, raw in enumerate(skill_dirs):
        src = Path(raw).resolve()
        if root is not None:
            try:
                rel = src.relative_to(root)
            except ValueError:
                rel = None
            else:
                mapped.append(f"{GUEST_WORKSPACE}/{rel.as_posix()}")
                continue
        name = src.name
        arc = f".apipi/skills/{name}"
        if arc in used:
            arc = f".apipi/skills/{name}-{index}"
        used.add(arc)
        extras.append((src, arc))
        mapped.append(f"{GUEST_WORKSPACE}/{arc}")
    return mapped, extras


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, *, mode: int) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = 0
    info.mode = mode
    tar.addfile(info, io.BytesIO(data))


def write_workspace_image(
    dest: Path,
    *,
    cwd: str | None,
    env: dict[str, str],
    pi_args: list[str],
    net: TapNet | None = None,
    extra_dirs: list[tuple[Path, str]] | None = None,
    shell: bool = False,
) -> None:
    guest_py = Path(__file__).with_name("guest.py").read_bytes()
    guest_sh = Path(__file__).with_name("guest.sh").read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if cwd:
            tar.add(str(Path(cwd).resolve()), arcname=".", recursive=True)
        for src, arcname in extra_dirs or []:
            tar.add(str(src), arcname=arcname, recursive=True)
        _add_bytes(tar, ".apipi/env", env_file(env).encode(), mode=0o600)
        if net is not None:
            _add_bytes(tar, ".apipi/net", net_file(net).encode(), mode=0o644)
        _add_bytes(tar, ".apipi/pi-args", json.dumps(pi_args).encode(), mode=0o644)
        _add_bytes(tar, ".apipi/pi-cmd", shlex.join(pi_args).encode(), mode=0o644)
        _add_bytes(tar, ".apipi/guest.py", guest_py, mode=0o755)
        _add_bytes(tar, ".apipi/guest.sh", guest_sh, mode=0o755)
        if shell:
            _add_bytes(tar, ".apipi/shell", b"", mode=0o644)
    data = buf.getvalue()
    extra = len(data) % 512
    if extra:
        data += b"\0" * (512 - extra)
    dest.write_bytes(data)


def microvm_config(
    *,
    kernel: str,
    rootfs: str,
    workspace: str,
    vsock: str,
    cid: int,
    net: TapNet,
    mem_mib: int = MEM_MIB,
    vcpus: int = VCPU_COUNT,
) -> dict[str, object]:
    return {
        "boot-source": {
            "kernel_image_path": kernel,
            "boot_args": boot_args(net),
        },
        "drives": [
            {
                "drive_id": "rootfs",
                "path_on_host": rootfs,
                "is_root_device": True,
                "is_read_only": True,
            },
            {
                "drive_id": "workspace",
                "path_on_host": workspace,
                "is_root_device": False,
                "is_read_only": True,
            },
        ],
        "machine-config": {
            "vcpu_count": vcpus,
            "mem_size_mib": mem_mib,
        },
        "vsock": {
            "guest_cid": cid,
            "uds_path": vsock,
        },
        "network-interfaces": [
            {
                "iface_id": "eth0",
                "guest_mac": net.mac,
                "host_dev_name": net.name,
            }
        ],
    }


def jailer_argv(
    *,
    jailer: str,
    firecracker: str,
    vm_id: str,
    uid: int,
    gid: int,
    chroot_base: str,
) -> list[str]:
    return [
        jailer,
        "--id",
        vm_id,
        "--exec-file",
        firecracker,
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        "--chroot-base-dir",
        chroot_base,
        "--",
        "--no-api",
        "--config-file",
        "config.json",
    ]


def _tap_chain(net: TapNet) -> str:
    return f"{net.name}eg"


def tap_setup_argv(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    uid: int,
    gid: int,
    tc: str | None = None,
    allowlist: bool = True,
    allowed_ips: list[str] | None = None,
    egress_mbit: int = 50,
) -> list[list[str]]:
    comment = f"apipi-{net.name}"
    chain = _tap_chain(net)
    cmds: list[list[str]] = [
        [
            ip,
            "tuntap",
            "add",
            "dev",
            net.name,
            "mode",
            "tap",
            "user",
            str(uid),
            "group",
            str(gid),
        ],
        [ip, "addr", "add", f"{net.host_ip}/{net.prefix}", "dev", net.name],
        [ip, "link", "set", "dev", net.name, "up"],
        [
            iptables,
            "-w",
            "-t",
            "nat",
            "-A",
            "POSTROUTING",
            "-s",
            f"{net.guest_ip}/32",
            "!",
            "-d",
            net.subnet,
            "-j",
            "MASQUERADE",
            "-m",
            "comment",
            "--comment",
            comment,
        ],
        [
            iptables,
            "-w",
            "-I",
            "FORWARD",
            "1",
            "-o",
            net.name,
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-m",
            "comment",
            "--comment",
            comment,
            "-j",
            "ACCEPT",
        ],
    ]
    if allowlist:
        cmds.append([iptables, "-w", "-N", chain])
        for dns in GUEST_DNS:
            cmds.append(
                [
                    iptables,
                    "-w",
                    "-A",
                    chain,
                    "-p",
                    "udp",
                    "-d",
                    dns,
                    "--dport",
                    "53",
                    "-j",
                    "ACCEPT",
                ]
            )
            cmds.append(
                [
                    iptables,
                    "-w",
                    "-A",
                    chain,
                    "-p",
                    "tcp",
                    "-d",
                    dns,
                    "--dport",
                    "53",
                    "-j",
                    "ACCEPT",
                ]
            )
        for dest in allowed_ips or []:
            cmds.append(
                [
                    iptables,
                    "-w",
                    "-A",
                    chain,
                    "-p",
                    "tcp",
                    "-d",
                    dest,
                    "-j",
                    "ACCEPT",
                ]
            )
        cmds.append(
            [
                iptables,
                "-w",
                "-A",
                chain,
                "-j",
                "REJECT",
                "--reject-with",
                "icmp-port-unreachable",
            ]
        )
        cmds.append(
            [
                iptables,
                "-w",
                "-I",
                "FORWARD",
                "1",
                "-i",
                net.name,
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                chain,
            ]
        )
    else:
        cmds.append(
            [
                iptables,
                "-w",
                "-I",
                "FORWARD",
                "1",
                "-i",
                net.name,
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ]
        )
    if tc is not None:
        rate = f"{egress_mbit}mbit"
        cmds.extend(
            [
                [
                    tc,
                    "qdisc",
                    "replace",
                    "dev",
                    net.name,
                    "root",
                    "tbf",
                    "rate",
                    rate,
                    "burst",
                    "64kb",
                    "latency",
                    "50ms",
                ],
                [
                    tc,
                    "qdisc",
                    "replace",
                    "dev",
                    net.name,
                    "handle",
                    "ffff:",
                    "ingress",
                ],
                [
                    tc,
                    "filter",
                    "replace",
                    "dev",
                    net.name,
                    "parent",
                    "ffff:",
                    "protocol",
                    "all",
                    "prio",
                    "1",
                    "u32",
                    "match",
                    "u32",
                    "0",
                    "0",
                    "police",
                    "rate",
                    rate,
                    "burst",
                    "64kb",
                    "drop",
                ],
            ]
        )
    return cmds


def tap_teardown_argv(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    tc: str | None = None,
    allowlist: bool = True,
) -> list[list[str]]:
    comment = f"apipi-{net.name}"
    chain = _tap_chain(net)
    cmds: list[list[str]] = []
    if tc is not None:
        cmds.append([tc, "qdisc", "del", "dev", net.name, "ingress"])
        cmds.append([tc, "qdisc", "del", "dev", net.name, "root"])
    if allowlist:
        cmds.extend(
            [
                [
                    iptables,
                    "-w",
                    "-D",
                    "FORWARD",
                    "-i",
                    net.name,
                    "-m",
                    "comment",
                    "--comment",
                    comment,
                    "-j",
                    chain,
                ],
                [iptables, "-w", "-F", chain],
                [iptables, "-w", "-X", chain],
            ]
        )
    else:
        cmds.append(
            [
                iptables,
                "-w",
                "-D",
                "FORWARD",
                "-i",
                net.name,
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ]
        )
    cmds.extend(
        [
            [
                iptables,
                "-w",
                "-D",
                "FORWARD",
                "-o",
                net.name,
                "-m",
                "conntrack",
                "--ctstate",
                "RELATED,ESTABLISHED",
                "-m",
                "comment",
                "--comment",
                comment,
                "-j",
                "ACCEPT",
            ],
            [
                iptables,
                "-w",
                "-t",
                "nat",
                "-D",
                "POSTROUTING",
                "-s",
                f"{net.guest_ip}/32",
                "!",
                "-d",
                net.subnet,
                "-j",
                "MASQUERADE",
                "-m",
                "comment",
                "--comment",
                comment,
            ],
            [ip, "link", "delete", "dev", net.name],
        ]
    )
    return cmds


def _enable_forward() -> None:
    path = Path("/proc/sys/net/ipv4/ip_forward")
    try:
        if path.read_text().strip() == "1":
            return
        path.write_text("1")
    except OSError as exc:
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc


def setup_tap(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    uid: int,
    gid: int,
    tc: str | None = None,
    allowlist: bool = True,
    allowed_ips: list[str] | None = None,
    egress_mbit: int = 50,
) -> None:
    _enable_forward()
    for argv in tap_setup_argv(
        net,
        ip=ip,
        iptables=iptables,
        uid=uid,
        gid=gid,
        tc=tc,
        allowlist=allowlist,
        allowed_ips=allowed_ips,
        egress_mbit=egress_mbit,
    ):
        _run(argv)


def teardown_tap(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    tc: str | None = None,
    allowlist: bool = True,
) -> None:
    for argv in tap_teardown_argv(
        net, ip=ip, iptables=iptables, tc=tc, allowlist=allowlist
    ):
        with contextlib.suppress(OSError):
            subprocess.run(argv, check=False, capture_output=True)


def _link_or_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copy2(src, dest)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def connect_vsock(
    path: Path,
    port: int,
    *,
    timeout: float = CONNECT_TIMEOUT,
    process: asyncio.subprocess.Process | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.returncode is not None:
            raise ConfigError("APIPI_RUN_MODE=microvm cannot start")
        try:
            reader, writer = await asyncio.open_unix_connection(str(path))
        except OSError as exc:
            last = exc
            await asyncio.sleep(0.05)
            continue
        try:
            writer.write(f"CONNECT {port}\n".encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=1)
        except (OSError, TimeoutError) as exc:
            last = exc
            await _close_writer(writer)
            await asyncio.sleep(0.05)
            continue
        if line.startswith(b"OK"):
            return reader, writer
        last = ConfigError("APIPI_RUN_MODE=microvm cannot start")
        await _close_writer(writer)
        await asyncio.sleep(0.05)
    raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from last


async def start_microvm(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    api_key: str | None = None,
    shell: bool = False,
    inherit_stdio: bool = False,
) -> StartedMicrovm:
    require_microvm(settings)
    firecracker, jailer = microvm_binaries()
    ip_bin, iptables_bin, tc_bin = microvm_net_binaries()
    kernel, rootfs = microvm_images(settings)
    vm_id = str(uuid.uuid4())
    net = tap_net(vm_id)
    uid = os.getuid()
    gid = os.getgid()
    work = Path(tempfile.mkdtemp(prefix="apipi-microvm-"))
    chroot_dir = work / Path(firecracker).name / vm_id / "root"
    chroot_dir.mkdir(parents=True)
    guest_skills, extra_dirs = guest_skill_dirs(cwd, skill_dirs)
    extra_dirs = [*(extra_dirs or []), (pi_agent_dir(settings), ".pi/agent")]
    allowlist = settings.microvm_egress_allowlist
    extra_hosts = workspace_egress_hosts(cwd)
    allowed_ips = (
        allowed_egress_ips(settings, mcp_http, extra_hosts=extra_hosts)
        if allowlist
        else []
    )
    stdio = None if inherit_stdio else asyncio.subprocess.DEVNULL

    def cleanup() -> None:
        teardown_tap(
            net,
            ip=ip_bin,
            iptables=iptables_bin,
            tc=tc_bin,
            allowlist=allowlist,
        )
        shutil.rmtree(work, ignore_errors=True)

    try:
        setup_tap(
            net,
            ip=ip_bin,
            iptables=iptables_bin,
            uid=uid,
            gid=gid,
            tc=tc_bin,
            allowlist=allowlist,
            allowed_ips=allowed_ips,
            egress_mbit=settings.microvm_egress_mbit,
        )
        _link_or_copy(Path(kernel), chroot_dir / "vmlinux")
        _link_or_copy(Path(rootfs), chroot_dir / "rootfs.ext4")
        write_workspace_image(
            chroot_dir / "workspace.tar",
            cwd=cwd,
            env=guest_env(settings, mcp_http, mcp_stdio, api_key=api_key),
            pi_args=pi_command_args(
                settings,
                tools=tools,
                mcp_http=mcp_http,
                mcp_stdio=mcp_stdio,
                skill_dirs=guest_skills,
                model=model,
                instructions=instructions,
            ),
            net=net,
            extra_dirs=extra_dirs,
            shell=shell,
        )
        config = microvm_config(
            kernel="vmlinux",
            rootfs="rootfs.ext4",
            workspace="workspace.tar",
            vsock=VSOCK_UDS,
            cid=guest_cid(vm_id),
            net=net,
            mem_mib=settings.microvm_mem_mib,
            vcpus=settings.microvm_vcpus,
        )
        (chroot_dir / "config.json").write_text(json.dumps(config))
        argv = jailer_argv(
            jailer=jailer,
            firecracker=firecracker,
            vm_id=vm_id,
            uid=uid,
            gid=gid,
            chroot_base=str(work),
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdio,
            stdout=stdio,
            stderr=stdio,
        )
    except (OSError, ConfigError) as exc:
        cleanup()
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc
    pid = process.pid
    if pid is None:
        process.kill()
        await process.wait()
        cleanup()
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start")
    return StartedMicrovm(process, chroot_dir, cleanup)


async def spawn_microvm_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    api_key: str | None = None,
) -> PiProc:
    started = await start_microvm(
        settings,
        cwd=cwd,
        tools=tools,
        mcp_http=mcp_http,
        mcp_stdio=mcp_stdio,
        skill_dirs=skill_dirs,
        model=model,
        instructions=instructions,
        api_key=api_key,
    )
    process = started.process
    try:
        reader, writer = await connect_vsock(
            started.vsock,
            VSOCK_PORT,
            process=process,
        )
    except (ConfigError, OSError) as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        started.cleanup()
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc

    async def _pull(port: int) -> bytes:
        art_reader, art_writer = await connect_vsock(
            started.vsock,
            port,
            timeout=5.0,
            process=process,
        )
        data = await art_reader.read()
        await _close_writer(art_writer)
        return data

    async def pull_artifacts() -> bytes:
        return await _pull(VSOCK_ARTIFACT_PORT)

    async def pull_workspace() -> bytes:
        return await _pull(VSOCK_WORKSPACE_PORT)

    return PiProc(
        process,
        stdin=writer,
        stdout=reader,
        on_stop=started.cleanup,
        pull_artifacts=pull_artifacts,
        pull_workspace=pull_workspace,
    )


async def run_microvm_shell(settings: Settings, *, cwd: str | None = None) -> int:
    if cwd is not None:
        path = Path(cwd).resolve()
        if not path.is_dir():
            raise ConfigError("apipi microvm shell --workspace must be a directory")
        cwd = str(path)
    started = await start_microvm(
        settings,
        cwd=cwd,
        tools=True,
        shell=True,
        inherit_stdio=True,
    )
    try:
        await started.process.wait()
        code = started.process.returncode
        return 0 if code == 0 else 1
    finally:
        if started.process.returncode is None:
            started.process.kill()
            await started.process.wait()
        started.cleanup()
