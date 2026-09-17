import asyncio
import contextlib
import errno
import io
import json
import logging
import os
import pwd
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

from apipi.config import ConfigError, Settings
from apipi.env.setup import (
    SetupError,
    tap_policy_from,
    workspace_egress_hosts,
    workspace_network_policy,
)
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.dirs import PI_SESSION_REL, pi_session_file
from apipi.pi.model_host import pi_agent_dir
from apipi.pi.proc import PiProc, pi_command_args, pi_env

VSOCK_PORT = 52
VSOCK_ARTIFACT_PORT = 53
VSOCK_WORKSPACE_PORT = 54
VSOCK_SESSION_PORT = 55
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
    "Operator microVM shell. Guest localhost and the public internet "
    "are open by default. The same TAP rate limit as agent sessions "
    "applies. Exit the shell or press Ctrl-C to stop the VM."
)
SHELL_SUDO_MARK = "APIPI_MICROVM_SHELL_SUDO"
SHELL_SUDO_NOTICE = "Need root for TAP, NAT, and jailer. Re-running under sudo."
NET_RIGHTS = (
    "Need root or CAP_NET_ADMIN (and CAP_NET_RAW) for TAP, NAT, and ip_forward."
)
JAILER_RIGHTS = "Need root to chroot Firecracker with jailer."
INSTALL_HINT = "Run apipi install and pick MicroVM"
log = logging.getLogger("apipi.microvm")


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
    broker: Any | None = None

    @property
    def vsock(self) -> Path:
        return self.chroot_dir / VSOCK_UDS


def kvm_available() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


def operator_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def xdg_cache_home() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME")
    if raw:
        return Path(raw)
    return operator_home() / ".cache"


def xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw)
    return operator_home() / ".local" / "share"


def microvm_image_dir() -> Path:
    return xdg_cache_home() / "apipi" / "microvm"


def firecracker_bin_dirs() -> list[Path]:
    dirs = [xdg_data_home() / "apipi" / "firecracker"]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            home = Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            home = None
        if home is not None:
            extra = home / ".local" / "share" / "apipi" / "firecracker"
            if extra not in dirs:
                dirs.append(extra)
    return dirs


def default_kernel_path() -> Path:
    return microvm_image_dir() / "vmlinux"


def default_rootfs_path() -> Path:
    return microvm_image_dir() / "rootfs.ext4"


def default_rootfs_browser_path() -> Path:
    return microvm_image_dir() / "rootfs-browser.ext4"


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found is not None:
        return found
    for folder in firecracker_bin_dirs():
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def _image_missing(name: str, path: Path) -> ConfigError:
    return ConfigError(
        f"microvm requires {name}. {INSTALL_HINT}, or set {name} / "
        f"[sandbox].{name.removeprefix('APIPI_MICROVM_').lower()}. Looked at {path}"
    )


def microvm_binaries() -> tuple[str, str]:
    firecracker = _find_binary("firecracker")
    if firecracker is None:
        raise ConfigError(
            f"microvm requires firecracker. {INSTALL_HINT}, or put firecracker on PATH"
        )
    jailer = _find_binary("jailer")
    if jailer is None:
        raise ConfigError(
            f"microvm requires jailer. {INSTALL_HINT}, or put jailer on PATH"
        )
    return firecracker, jailer


def microvm_net_binaries() -> tuple[str, str, str]:
    ip = shutil.which("ip")
    if ip is None:
        raise ConfigError("microvm requires ip (install iproute2)")
    iptables = shutil.which("iptables")
    if iptables is None:
        raise ConfigError("microvm requires iptables (install iptables)")
    tc = shutil.which("tc")
    if tc is None:
        raise ConfigError("microvm requires tc (install iproute2)")
    return ip, iptables, tc


def microvm_image_name(settings: Settings | None = None) -> str:
    if settings is not None:
        return settings.microvm_image
    raw = os.environ.get("APIPI_MICROVM_IMAGE", "default")
    image = raw.strip() or "default"
    if image not in {"default", "browser"}:
        raise ConfigError("APIPI_MICROVM_IMAGE must be default or browser")
    return image


def _resolve_image_file(configured: str | None, default: Path, name: str) -> str:
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise ConfigError(
            f"microvm requires {name} ({path} is not a file). {INSTALL_HINT}, "
            f"or set {name} / [sandbox].{name.removeprefix('APIPI_MICROVM_').lower()}"
        )
    if default.is_file():
        return str(default)
    raise _image_missing(name, default)


def microvm_images(
    settings: Settings | None = None, *, image: str | None = None
) -> tuple[str, str]:
    if settings is not None:
        kernel = settings.microvm_kernel
        default_rootfs = settings.microvm_rootfs
        browser_rootfs = settings.microvm_rootfs_browser
    else:
        kernel = os.environ.get("APIPI_MICROVM_KERNEL")
        default_rootfs = os.environ.get("APIPI_MICROVM_ROOTFS")
        browser_rootfs = os.environ.get("APIPI_MICROVM_ROOTFS_BROWSER")
    kernel_path = _resolve_image_file(
        kernel, default_kernel_path(), "APIPI_MICROVM_KERNEL"
    )
    selected = image if image is not None else microvm_image_name(settings)
    if selected == "browser":
        rootfs_path = _resolve_image_file(
            browser_rootfs,
            default_rootfs_browser_path(),
            "APIPI_MICROVM_ROOTFS_BROWSER",
        )
        return kernel_path, rootfs_path
    rootfs_path = _resolve_image_file(
        default_rootfs, default_rootfs_path(), "APIPI_MICROVM_ROOTFS"
    )
    return kernel_path, rootfs_path


def microvm_shell_needs_sudo() -> bool:
    if os.geteuid() == 0:
        return False
    return os.environ.get(SHELL_SUDO_MARK) != "1"


def microvm_shell_sudo_argv(
    extra: list[str],
    *,
    executable: str | None = None,
    home: str | None = None,
    path: str | None = None,
) -> list[str]:
    argv = [
        "sudo",
        "-E",
        "env",
        f"PATH={path if path is not None else os.environ.get('PATH', '')}",
        f"HOME={home if home is not None else os.environ.get('HOME', '')}",
        f"{SHELL_SUDO_MARK}=1",
    ]
    for key in ("XDG_CACHE_HOME", "XDG_DATA_HOME"):
        value = os.environ.get(key)
        if value:
            argv.append(f"{key}={value}")
    argv.extend(
        [
            executable if executable is not None else sys.executable,
            "-m",
            "apipi",
            "microvm",
            "shell",
            *extra,
        ]
    )
    return argv


def reexec_microvm_shell(extra: list[str]) -> None:
    argv = microvm_shell_sudo_argv(extra)
    print(SHELL_SUDO_NOTICE, file=sys.stderr)
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        raise ConfigError(
            f"microvm shell needs sudo on PATH to create TAP devices: {exc}"
        ) from exc


def require_microvm(settings: Settings | None = None) -> None:
    if not kvm_available():
        raise ConfigError("microvm requires /dev/kvm")
    microvm_binaries()
    microvm_net_binaries()
    microvm_images(settings, image="default")
    if settings is not None and settings.sandbox_default_size == "L":
        microvm_images(settings, image="browser")


async def probe_microvm(settings: Settings) -> None:
    from apipi.sandbox import image_for_size

    size = settings.sandbox_default_size
    proc = await spawn_microvm_pi(
        settings,
        cwd=None,
        tools=False,
        mem_mib=settings.sandbox_mem_mib(size),
        image=image_for_size(size),
    )
    try:
        if not proc.alive:
            raise ConfigError("microvm cannot start")
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
        raise ConfigError(f"microvm cannot resolve {host}") from exc
    ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        ip = info[4][0]
        if not isinstance(ip, str) or ip in seen:
            continue
        seen.add(ip)
        ips.append(ip)
    if not ips:
        raise ConfigError(f"microvm cannot resolve {host}")
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
    broker: object | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = pi_env(
        settings,
        mcp_http,
        mcp_stdio,
        api_key=api_key,
        broker=broker,
        extra_env=extra_env,
    )
    env["PI_CODING_AGENT_DIR"] = f"{GUEST_WORKSPACE}/.pi/agent"
    extra_keys = set(extra_env) if extra_env else set()
    return {
        key: value
        for key, value in env.items()
        if key.startswith("OPENAI_")
        or key.startswith("APIPI_")
        or key == "PI_CODING_AGENT_DIR"
        or key in extra_keys
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
    models_json: bytes | None = None,
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
        if models_json is not None:
            _add_bytes(tar, ".pi/agent/models.json", models_json, mode=0o644)
        _add_bytes(tar, ".apipi/random", os.urandom(256), mode=0o600)
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
        "entropy": {},
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
    allowlist: bool = False,
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
    allowlist: bool = False,
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


def _exc_detail(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        raw = exc.stderr if exc.stderr else exc.stdout
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", "replace").strip()
        elif raw:
            text = str(raw).strip()
        else:
            text = ""
        return text or f"exit {exc.returncode}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc).strip()


def _permission_denied(detail: str, exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError) and exc.errno in {errno.EPERM, errno.EACCES}:
        return True
    text = detail.lower()
    return "operation not permitted" in text or "permission denied" in text


def _host_step(argv: list[str]) -> str:
    name = Path(argv[0]).name if argv else "command"
    if name == "ip" and "tuntap" in argv:
        return "create a TAP device"
    if name == "ip":
        return "configure the TAP device"
    if name == "iptables":
        return "add iptables NAT or filter rules"
    if name == "tc":
        return "rate-limit TAP egress with tc"
    return f"run {name}"


def _host_cmd_error(argv: list[str], exc: BaseException) -> ConfigError:
    detail = _exc_detail(exc)
    step = _host_step(argv)
    if _permission_denied(detail, exc):
        return ConfigError(f"microvm cannot {step}: {detail}. {NET_RIGHTS}")
    return ConfigError(f"microvm cannot {step}: {detail}")


def _enable_forward() -> None:
    path = Path("/proc/sys/net/ipv4/ip_forward")
    try:
        if path.read_text().strip() == "1":
            return
        path.write_text("1")
    except OSError as exc:
        detail = _exc_detail(exc)
        if _permission_denied(detail, exc):
            raise ConfigError(
                f"microvm cannot set ip_forward: {detail}. {NET_RIGHTS}"
            ) from exc
        raise ConfigError(f"microvm cannot set ip_forward: {detail}") from exc


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(argv, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _host_cmd_error(argv, exc) from exc


def setup_tap(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    uid: int,
    gid: int,
    tc: str | None = None,
    allowlist: bool = False,
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
    allowlist: bool = False,
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


async def _log_console(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n\r")
        if text:
            log.debug("console", extra={"line": text[:500]})


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
            raise ConfigError("microvm cannot start")
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
            log.info("vsock", extra={"port": port})
            return reader, writer
        last = ConfigError("microvm cannot start")
        await _close_writer(writer)
        await asyncio.sleep(0.05)
    raise ConfigError("microvm cannot start") from last


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
    mem_mib: int | None = None,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> StartedMicrovm:
    require_microvm(settings)
    firecracker, jailer = microvm_binaries()
    ip_bin, iptables_bin, tc_bin = microvm_net_binaries()
    selected = image if image is not None else microvm_image_name(settings)
    kernel, rootfs = microvm_images(settings, image=selected)
    guest_mem = mem_mib if mem_mib is not None else settings.microvm_mem_mib
    vm_id = str(uuid.uuid4())
    net = tap_net(vm_id)
    uid = os.getuid()
    gid = os.getgid()
    work = Path(tempfile.mkdtemp(prefix="apipi-microvm-"))
    chroot_dir = work / Path(firecracker).name / vm_id / "root"
    chroot_dir.mkdir(parents=True)
    guest_skills, extra_dirs = guest_skill_dirs(cwd, skill_dirs)
    extra_dirs = [*(extra_dirs or []), (pi_agent_dir(settings), ".pi/agent")]
    extra_hosts = workspace_egress_hosts(cwd)
    try:
        policy = workspace_network_policy(cwd)
        tap = tap_policy_from(
            policy,
            gateway_allowlist=settings.microvm_egress_allowlist,
            gateway_hosts=tuple(microvm_egress_hosts(settings, mcp_http)),
            extra_hosts=tuple(extra_hosts),
        )
    except SetupError as exc:
        raise ConfigError(exc.message) from exc
    allowlist = tap.allowlist
    allowed_ips: list[str] = []
    if allowlist:
        seen_ips: set[str] = set()
        for host in tap.hosts:
            for ip in resolve_host_ips(host):
                if ip in seen_ips:
                    continue
                seen_ips.add(ip)
                allowed_ips.append(ip)
    stdio_in = None if inherit_stdio else asyncio.subprocess.DEVNULL
    stdio_out = None if inherit_stdio else asyncio.subprocess.PIPE
    console_tasks: list[asyncio.Task[None]] = []
    log.info(
        "boot",
        extra={"vm_id": vm_id, "tap": net.name, "shell": shell},
    )

    def cleanup() -> None:
        for task in console_tasks:
            task.cancel()
        teardown_tap(
            net,
            ip=ip_bin,
            iptables=iptables_bin,
            tc=tc_bin,
            allowlist=allowlist,
        )
        shutil.rmtree(work, ignore_errors=True)

    broker = None
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
        from apipi.broker import start_broker
        from apipi.pi.model_host import models_json_for_base_url

        broker = await start_broker(
            settings,
            api_key=api_key,
            mcp_http=mcp_http,
            host="0.0.0.0",
            port=0,
            public_host=net.host_ip,
        )
        _link_or_copy(Path(kernel), chroot_dir / "vmlinux")
        _link_or_copy(Path(rootfs), chroot_dir / "rootfs.ext4")
        if cwd:
            pi_session_file(Path(cwd)).parent.mkdir(parents=True, exist_ok=True)
        write_workspace_image(
            chroot_dir / "workspace.tar",
            cwd=cwd,
            env=guest_env(
                settings,
                mcp_http,
                mcp_stdio,
                api_key=api_key,
                broker=broker,
                extra_env=extra_env,
            ),
            pi_args=pi_command_args(
                settings,
                tools=tools,
                mcp_http=mcp_http,
                mcp_stdio=mcp_stdio,
                skill_dirs=guest_skills,
                model=model,
                instructions=instructions,
                session_file=PI_SESSION_REL if cwd else None,
            ),
            net=net,
            extra_dirs=extra_dirs,
            shell=shell,
            models_json=models_json_for_base_url(settings, broker.openai_base_url),
        )
        config = microvm_config(
            kernel="vmlinux",
            rootfs="rootfs.ext4",
            workspace="workspace.tar",
            vsock=VSOCK_UDS,
            cid=guest_cid(vm_id),
            net=net,
            mem_mib=guest_mem,
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
            stdin=stdio_in,
            stdout=stdio_out,
            stderr=stdio_out,
        )
    except (OSError, ConfigError, RuntimeError, ValueError) as exc:
        if broker is not None:
            await broker.stop()
        cleanup()
        if isinstance(exc, ConfigError):
            raise
        if isinstance(exc, (RuntimeError, ValueError)):
            raise ConfigError(str(exc)) from exc
        detail = _exc_detail(exc)
        if _permission_denied(detail, exc):
            raise ConfigError(
                f"microvm cannot start jailer: {detail}. {JAILER_RIGHTS}"
            ) from exc
        raise ConfigError(f"microvm cannot start jailer: {detail}") from exc
    pid = process.pid
    if pid is None:
        process.kill()
        await process.wait()
        if broker is not None:
            await broker.stop()
        cleanup()
        raise ConfigError("microvm cannot start jailer")
    log.info("jailer", extra={"vm_id": vm_id, "pid": pid})
    if not inherit_stdio:
        console_tasks.append(asyncio.create_task(_log_console(process.stdout)))
        console_tasks.append(asyncio.create_task(_log_console(process.stderr)))
    return StartedMicrovm(process, chroot_dir, cleanup, broker)


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
    mem_mib: int | None = None,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
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
        mem_mib=mem_mib,
        image=image,
        extra_env=extra_env,
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
        extra = started.broker
        if extra is not None:
            await extra.stop()
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("microvm cannot start") from exc

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

    async def pull_session() -> bytes:
        return await _pull(VSOCK_SESSION_PORT)

    return PiProc(
        process,
        stdin=writer,
        stdout=reader,
        on_stop=started.cleanup,
        broker=started.broker,
        pull_artifacts=pull_artifacts,
        pull_workspace=pull_workspace,
        pull_session=pull_session,
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
