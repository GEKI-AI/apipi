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

from apipi.common.dirs import (
    PI_SESSION_REL,
    pi_session_file,
    xdg_data_home,
)
from apipi.common.logutil import log_event
from apipi.config import ConfigError, Settings
from apipi.env.setup import (
    SetupError,
    tap_policy_from,
    workspace_egress_hosts,
    workspace_network_policy,
)
from apipi.mcp.http import McpHttpServer
from apipi.worker.egress import (
    BLOCKED_EGRESS_CIDRS,
    GATEWAY_PORTS,
    EgressGateway,
    EgressHooks,
    EgressMode,
    start_gateway,
    upstream_context,
)
from apipi.worker.pi.extension import (
    APIPI_EXTENSION_REL,
    MCP_EXTENSION_REL,
    WEB_SEARCH_EXTENSION_REL,
    apipi_extension_source,
    guest_extensions,
    mcp_extension_source,
    web_search_extension_source,
)
from apipi.worker.pi.model_host import pi_agent_dir
from apipi.worker.pi.proc import PiProc, pi_command_args, pi_env

VSOCK_PORT = 52
VSOCK_ARTIFACT_PORT = 53
VSOCK_WORKSPACE_PORT = 54
VSOCK_SESSION_PORT = 55
VSOCK_METRICS_PORT = 56
VSOCK_PUSH_PORT = 57
PUSH_TIMEOUT = 60.0
VSOCK_UDS = "vsock.sock"
MEM_MIB = 512
VCPU_COUNT = 1
CONNECT_TIMEOUT = 30.0
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/apipi-guest"
GUEST_WORKSPACE = "/workspace"
GUEST_DNS = ("1.1.1.1", "8.8.8.8")
EGRESS_CA_REL = ".apipi/egress-ca.pem"
TAP_NET_BASE = 0xAC100000
TAP_NET_SLOTS = 16384
SHELL_WARNING = (
    "Operator microVM shell. Guest localhost and the public internet "
    "are open. Private and special-use IPv4 ranges are rejected. "
    "The same TAP rate limit as agent sessions applies. "
    "Exit the shell or press Ctrl-C to stop the VM."
)
SHELL_SUDO_MARK = "APIPI_MICROVM_SHELL_SUDO"
SHELL_SUDO_NOTICE = "Need root for TAP, NAT, and jailer. Re-running under sudo."
NET_RIGHTS = (
    "Need root or CAP_NET_ADMIN (and CAP_NET_RAW) for TAP, NAT, and ip_forward."
)
JAILER_RIGHTS = "Need root to chroot Firecracker with jailer."
INSTALL_HINT = "Run apipi install and pick MicroVM"
log = logging.getLogger("apipi.microvm")


def log_sandbox_boot_failed(exc: BaseException, *, vm_id: str | None = None) -> None:
    log_event(
        log,
        logging.ERROR,
        "sandbox boot failed",
        event="sandbox.boot.failed",
        error_code="sandbox_boot_failed",
        failure_source="internal",
        retryable=True,
        exc_info=exc,
        vm_id=vm_id,
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


class TapPorts(NamedTuple):
    broker: int
    gateway: int | None = None
    dns_udp: int | None = None
    dns_tcp: int | None = None


class ResolvedImage(NamedTuple):
    id: str | None
    version: str | None
    digest: str | None


class StartedMicrovm(NamedTuple):
    process: asyncio.subprocess.Process
    chroot_dir: Path
    cleanup: Callable[[], None]
    broker: Any | None = None
    image: ResolvedImage | None = None
    egress: EgressGateway | None = None

    @property
    def vsock(self) -> Path:
        return self.chroot_dir / VSOCK_UDS


def kvm_available() -> bool:
    return os.access("/dev/kvm", os.R_OK | os.W_OK)


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


def _find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found is not None:
        return found
    for folder in firecracker_bin_dirs():
        candidate = folder / name
        if candidate.is_file():
            return str(candidate)
    return None


def _image_missing(name: str) -> ConfigError:
    return ConfigError(
        f"microvm requires {name}. {INSTALL_HINT}, or set {name} / "
        f"[sandbox].{name.removeprefix('APIPI_MICROVM_').lower()} as a dev override. "
        "Otherwise run apipi images pull <id>."
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


def _resolve_override_file(configured: str | None, name: str) -> str | None:
    if not configured:
        return None
    path = Path(configured)
    if path.is_file():
        return str(path)
    raise ConfigError(
        f"microvm requires {name} ({path} is not a file). {INSTALL_HINT}, "
        f"or set {name} / [sandbox].{name.removeprefix('APIPI_MICROVM_').lower()} "
        "to a dev override file."
    )


def _images_dir_file(settings: Settings, image_id: str) -> Path | None:
    from apipi.common.images import read_current
    from apipi.worker.pi.image_pull import configured_images_dir

    root = configured_images_dir(settings)
    version = read_current(root, image_id)
    if version is None:
        return None
    path = root / image_id / version / "rootfs.ext4"
    return path if path.is_file() else None


def kernel_for_image(settings: Settings, image_id: str) -> Path | None:
    from apipi.common.images import local_kernel_path, read_current
    from apipi.worker.pi.image_pull import configured_images_dir

    root = configured_images_dir(settings)
    version = read_current(root, image_id)
    if version is None:
        return None
    manifest = root / image_id / version / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    kernel = data.get("kernel") if isinstance(data, dict) else None
    kernel_version = kernel.get("version") if isinstance(kernel, dict) else None
    arch = data.get("arch") if isinstance(data, dict) else None
    if isinstance(kernel_version, str) and isinstance(arch, str):
        versioned = root / "kernels" / arch / kernel_version / "vmlinux"
        if versioned.is_file():
            return versioned
    path = local_kernel_path(root, os.uname().machine)
    return path if path.is_file() else None


def _images_dir_kernel(settings: Settings) -> Path | None:
    from apipi.common.images import local_kernel_path
    from apipi.worker.pi.image_pull import configured_images_dir

    path = local_kernel_path(configured_images_dir(settings), os.uname().machine)
    return path if path.is_file() else None


def _digest_from_manifest(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"sandbox image manifest is invalid: {path}") from exc
    rootfs = data.get("rootfs") if isinstance(data, dict) else None
    digest = rootfs.get("sha256") if isinstance(rootfs, dict) else None
    if not isinstance(digest, str) or not digest:
        raise ConfigError(f"sandbox image manifest missing rootfs.sha256: {path}")
    return digest


def resolve_spawn_image(
    settings: Settings, image: str | None, rootfs: str
) -> ResolvedImage:
    selected = image if image is not None else settings.sandbox_default_image
    from apipi.common.images import read_current
    from apipi.worker.pi.image_pull import configured_images_dir

    root = configured_images_dir(settings)
    version = read_current(root, selected)
    if version is not None:
        pulled = root / selected / version / "rootfs.ext4"
        if pulled.is_file():
            try:
                if Path(rootfs).resolve() == pulled.resolve():
                    digest = _digest_from_manifest(pulled.parent / "manifest.json")
                    return ResolvedImage(id=selected, version=version, digest=digest)
            except OSError:
                pass
    return ResolvedImage(id=selected, version=None, digest=None)


def microvm_images(settings: Settings, *, image: str | None = None) -> tuple[str, str]:
    selected = image if image is not None else settings.sandbox_default_image
    override_kernel = _resolve_override_file(
        settings.microvm_kernel, "APIPI_MICROVM_KERNEL"
    )
    if override_kernel is not None:
        kernel_path = override_kernel
    else:
        pulled = kernel_for_image(settings, selected) or _images_dir_kernel(settings)
        if pulled is None:
            raise _image_missing("APIPI_MICROVM_KERNEL")
        kernel_path = str(pulled)
    override_rootfs = _resolve_override_file(
        settings.microvm_rootfs, "APIPI_MICROVM_ROOTFS"
    )
    if override_rootfs is not None:
        return kernel_path, override_rootfs
    pulled_root = _images_dir_file(settings, selected)
    if pulled_root is not None:
        return kernel_path, str(pulled_root)
    raise ConfigError(
        f"sandbox_image {selected} is not in the images dir. "
        f"Run apipi images pull {selected}."
    )


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


def require_microvm(settings: Settings | None) -> None:
    if not kvm_available():
        raise ConfigError("microvm requires /dev/kvm")
    if settings is None:
        raise ConfigError("microvm requires settings")
    microvm_binaries()
    microvm_net_binaries()
    upstream_ca = settings.microvm_egress_upstream_ca
    if upstream_ca:
        if not Path(upstream_ca).is_file():
            raise ConfigError(
                f"APIPI_MICROVM_EGRESS_UPSTREAM_CA is not a file: {upstream_ca}"
            )
        try:
            upstream_context(upstream_ca)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"APIPI_MICROVM_EGRESS_UPSTREAM_CA is not a valid PEM bundle: "
                f"{upstream_ca}"
            ) from exc
    microvm_images(settings, image=settings.sandbox_default_image)
    if settings.sandbox_default_size == "L":
        microvm_images(settings, image="browser")


async def probe_microvm(settings: Settings) -> None:
    from apipi.common.sandbox import image_for_size

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


def guest_vcpus(
    settings: Settings, mem_mib: int | None, image: str | None = None
) -> int:
    from apipi.common.sandbox import min_vcpus_for_image, size_for_mem

    guest_mem = mem_mib if mem_mib is not None else settings.microvm_mem_mib
    base = settings.sandbox_vcpus(size_for_mem(settings, guest_mem))
    return max(base, min_vcpus_for_image(image, settings))


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


_GUEST_FILE_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "PI_CODING_AGENT_DIR",
        "NODE_OPTIONS",
        "APIPI_PINNED_PI",
        "APIPI_MCP_SERVERS",
        "APIPI_SEARCH_URL",
    }
)
_MCP_HTTP_FIELDS = frozenset({"LABEL", "URL", "ALLOWED"})
_EXTRA_NEVER = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY_OVERWRITE",
        "DATABASE_URL",
        "PI_CODING_AGENT_DIR",
        "NODE_OPTIONS",
        "APIPI_SEARCH_URL",
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
    }
)


def _indexed_mcp_key(key: str, prefix: str, fields: frozenset[str]) -> bool:
    if not key.startswith(prefix):
        return False
    index, sep, suffix = key.removeprefix(prefix).partition("_")
    return bool(sep) and index.isdigit() and suffix in fields


def _guest_file_key(key: str) -> bool:
    if key in _GUEST_FILE_KEYS:
        return True
    if _indexed_mcp_key(key, "APIPI_MCP_", _MCP_HTTP_FIELDS):
        return True
    if key.startswith(("APIPI_", "OPENAI_", "CODEX_", "PI_")):
        return False
    return key not in _EXTRA_NEVER


def _take(dest: dict[str, str], src: dict[str, str], key: str) -> None:
    value = src.get(key)
    if value is not None:
        dest[key] = value


def guest_env(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    *,
    api_key: str | None = None,
    broker: object | None = None,
    extra_env: dict[str, str] | None = None,
    web_search: bool = False,
) -> dict[str, str]:
    built = pi_env(
        settings,
        mcp_http,
        api_key=api_key,
        broker=broker,
        extra_env=extra_env,
        web_search=web_search,
    )
    guest: dict[str, str] = {
        "PI_CODING_AGENT_DIR": f"{GUEST_WORKSPACE}/.pi/agent",
    }
    _take(guest, built, "APIPI_PINNED_PI")
    if settings.pi_mem_mib is not None:
        _take(guest, built, "NODE_OPTIONS")
    if broker is not None:
        from apipi.worker.pi.broker import DUMMY_KEY

        guest["OPENAI_API_KEY"] = DUMMY_KEY
        base = getattr(broker, "openai_base_url", None)
        if isinstance(base, str) and base:
            guest["OPENAI_BASE_URL"] = base
    if mcp_http:
        _take(guest, built, "APIPI_MCP_SERVERS")
        for index, _server in enumerate(mcp_http):
            _take(guest, built, f"APIPI_MCP_{index}_LABEL")
            _take(guest, built, f"APIPI_MCP_{index}_URL")
            _take(guest, built, f"APIPI_MCP_{index}_ALLOWED")
    if web_search:
        _take(guest, built, "APIPI_SEARCH_URL")
    if extra_env:
        for key, value in extra_env.items():
            if key in guest or key in _EXTRA_NEVER or not _guest_file_key(key):
                continue
            guest[key] = value
    return {key: value for key, value in guest.items() if _guest_file_key(key)}


def env_file(env: dict[str, str]) -> str:
    lines = [
        f"{key}={shlex.quote(value)}"
        for key, value in env.items()
        if _guest_file_key(key)
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


def push_tar_bytes(files: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path, data in files:
            _add_bytes(tar, path, data, mode=0o644)
    return buf.getvalue()


async def push_workspace_files(
    vsock: Path,
    files: list[tuple[str, bytes]],
    *,
    process: asyncio.subprocess.Process | None = None,
    timeout: float = PUSH_TIMEOUT,
) -> None:
    """Copy files into the workspace of a running guest.

    The host sends a line with the size and then a tar of that size on
    the push port. The guest replaces each file under `/workspace` and
    answers `OK`. The whole exchange must end within `timeout`. Raises
    `ConfigError` when the guest does not answer `OK`.
    """
    data = push_tar_bytes(files)
    async with asyncio.timeout(timeout):
        reader, writer = await connect_vsock(
            vsock, VSOCK_PUSH_PORT, timeout=5.0, process=process
        )
        try:
            writer.write(f"{len(data)}\n".encode())
            writer.write(data)
            await writer.drain()
            line = await reader.readline()
        finally:
            await _close_writer(writer)
    if not line.startswith(b"OK"):
        raise ConfigError("microvm guest did not take the files")


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
    settings_json: bytes | None = None,
    system_md: bytes | None = None,
    web_search: bool = False,
    egress_ca: bytes | None = None,
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
        if settings_json is not None:
            _add_bytes(tar, ".pi/agent/settings.json", settings_json, mode=0o644)
        if system_md is not None:
            _add_bytes(tar, ".pi/agent/SYSTEM.md", system_md, mode=0o644)
        _add_bytes(tar, APIPI_EXTENSION_REL, apipi_extension_source(), mode=0o644)
        _add_bytes(tar, MCP_EXTENSION_REL, mcp_extension_source(), mode=0o644)
        if web_search:
            _add_bytes(
                tar,
                WEB_SEARCH_EXTENSION_REL,
                web_search_extension_source(),
                mode=0o644,
            )
        if egress_ca is not None:
            _add_bytes(tar, EGRESS_CA_REL, egress_ca, mode=0o644)
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


def _nat_chain(net: TapNet) -> str:
    return f"{net.name}gw"


def _input_chain(net: TapNet) -> str:
    return f"{net.name}in"


def _reject_unreachable(iptables: str, chain: str, *match: str) -> list[str]:
    return [
        iptables,
        "-w",
        "-A",
        chain,
        *match,
        "-j",
        "REJECT",
        "--reject-with",
        "icmp-port-unreachable",
    ]


def _egress_filter_cmds(
    iptables: str,
    chain: str,
    *,
    subnet: str,
    mode: EgressMode,
) -> list[list[str]]:
    cmds: list[list[str]] = [
        [iptables, "-w", "-N", chain],
        [iptables, "-w", "-A", chain, "-d", subnet, "-j", "ACCEPT"],
    ]
    if mode != "disabled":
        cmds.append(_reject_unreachable(iptables, chain, "-p", "udp", "--dport", "443"))
    for cidr in BLOCKED_EGRESS_CIDRS:
        cmds.append(_reject_unreachable(iptables, chain, "-d", cidr))
    if mode == "enabled":
        cmds.append([iptables, "-w", "-A", chain, "-j", "ACCEPT"])
        return cmds
    cmds.append(_reject_unreachable(iptables, chain))
    return cmds


def _input_cmds(
    iptables: str, chain: str, *, net: TapNet, ports: TapPorts
) -> list[list[str]]:
    cmds: list[list[str]] = [
        [iptables, "-w", "-N", chain],
        [
            iptables,
            "-w",
            "-A",
            chain,
            "-m",
            "conntrack",
            "--ctstate",
            "RELATED,ESTABLISHED",
            "-j",
            "ACCEPT",
        ],
    ]
    allowed = [("tcp", ports.broker), ("tcp", ports.gateway)]
    allowed += [("udp", ports.dns_udp), ("tcp", ports.dns_tcp)]
    for proto, port in allowed:
        if port is None:
            continue
        cmds.append(
            [
                iptables,
                "-w",
                "-A",
                chain,
                "-d",
                net.host_ip,
                "-p",
                proto,
                "--dport",
                str(port),
                "-j",
                "ACCEPT",
            ]
        )
    cmds.append(_reject_unreachable(iptables, chain))
    return cmds


def _input_jump(
    iptables: str, net: TapNet, action: list[str], comment: str
) -> list[str]:
    return [
        iptables,
        "-w",
        *action,
        "-i",
        net.name,
        "-m",
        "comment",
        "--comment",
        comment,
        "-j",
        _input_chain(net),
    ]


def _dnat(iptables: str, chain: str, proto: str, ports: str, target: str) -> list[str]:
    match = (
        ["-m", "multiport", "--dports", ports] if "," in ports else ["--dport", ports]
    )
    return [
        iptables,
        "-w",
        "-t",
        "nat",
        "-A",
        chain,
        "-p",
        proto,
        *match,
        "-j",
        "DNAT",
        "--to-destination",
        target,
    ]


def _gateway_nat_cmds(
    iptables: str,
    chain: str,
    *,
    net: TapNet,
    mode: EgressMode,
    ports: TapPorts,
) -> list[list[str]]:
    if mode == "disabled":
        return []
    if ports.gateway is None:
        raise ValueError("microvm egress gateway is not running")
    web = ",".join(str(port) for port in GATEWAY_PORTS)
    cmds: list[list[str]] = [
        [iptables, "-w", "-t", "nat", "-N", chain],
        [
            iptables,
            "-w",
            "-t",
            "nat",
            "-A",
            chain,
            "-d",
            net.subnet,
            "-j",
            "RETURN",
        ],
        _dnat(iptables, chain, "tcp", web, f"{net.host_ip}:{ports.gateway}"),
    ]
    if mode == "restricted":
        if ports.dns_udp is None or ports.dns_tcp is None:
            raise ValueError("microvm egress DNS filter is not running")
        cmds.append(
            _dnat(iptables, chain, "udp", "53", f"{net.host_ip}:{ports.dns_udp}")
        )
        cmds.append(
            _dnat(iptables, chain, "tcp", "53", f"{net.host_ip}:{ports.dns_tcp}")
        )
    return cmds


def _gateway_jump(
    iptables: str, net: TapNet, action: list[str], comment: str
) -> list[str]:
    return [
        iptables,
        "-w",
        "-t",
        "nat",
        *action,
        "-i",
        net.name,
        "-m",
        "comment",
        "--comment",
        comment,
        "-j",
        _nat_chain(net),
    ]


def tap_ports(broker_port: int, gateway: EgressGateway | None) -> TapPorts:
    if gateway is None:
        return TapPorts(broker=broker_port)
    dns = gateway.dns_ports
    if dns is None:
        return TapPorts(broker=broker_port, gateway=gateway.port)
    return TapPorts(
        broker=broker_port, gateway=gateway.port, dns_udp=dns[0], dns_tcp=dns[1]
    )


def tap_setup_argv(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    uid: int,
    gid: int,
    ports: TapPorts,
    tc: str | None = None,
    mode: EgressMode = "enabled",
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
    cmds.extend(_egress_filter_cmds(iptables, chain, subnet=net.subnet, mode=mode))
    cmds.extend(_input_cmds(iptables, _input_chain(net), net=net, ports=ports))
    cmds.append(_input_jump(iptables, net, ["-I", "INPUT", "1"], comment))
    nat = _gateway_nat_cmds(iptables, _nat_chain(net), net=net, mode=mode, ports=ports)
    if nat:
        cmds.extend(nat)
        cmds.append(_gateway_jump(iptables, net, ["-I", "PREROUTING", "1"], comment))
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
    mode: EgressMode = "enabled",
) -> list[list[str]]:
    comment = f"apipi-{net.name}"
    chain = _tap_chain(net)
    cmds: list[list[str]] = []
    if tc is not None:
        cmds.append([tc, "qdisc", "del", "dev", net.name, "ingress"])
        cmds.append([tc, "qdisc", "del", "dev", net.name, "root"])
    if mode != "disabled":
        nat_chain = _nat_chain(net)
        cmds.extend(
            [
                _gateway_jump(iptables, net, ["-D", "PREROUTING"], comment),
                [iptables, "-w", "-t", "nat", "-F", nat_chain],
                [iptables, "-w", "-t", "nat", "-X", nat_chain],
            ]
        )
    input_chain = _input_chain(net)
    cmds.extend(
        [
            _input_jump(iptables, net, ["-D", "INPUT"], comment),
            [iptables, "-w", "-F", input_chain],
            [iptables, "-w", "-X", input_chain],
        ]
    )
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
    ports: TapPorts,
    tc: str | None = None,
    mode: EgressMode = "enabled",
    egress_mbit: int = 50,
) -> None:
    _enable_forward()
    for argv in tap_setup_argv(
        net,
        ip=ip,
        iptables=iptables,
        uid=uid,
        gid=gid,
        ports=ports,
        tc=tc,
        mode=mode,
        egress_mbit=egress_mbit,
    ):
        _run(argv)


def teardown_tap(
    net: TapNet,
    *,
    ip: str,
    iptables: str,
    tc: str | None = None,
    mode: EgressMode = "enabled",
) -> None:
    for argv in tap_teardown_argv(net, ip=ip, iptables=iptables, tc=tc, mode=mode):
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


def _console_level(text: str) -> int:
    if text.startswith("mcp:") or text.startswith("mcp "):
        return logging.INFO
    return logging.DEBUG


async def _log_console(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").rstrip("\n\r")
        if text:
            clipped = text[:500]
            log.log(_console_level(clipped), "console", extra={"line": clipped})


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
    function_tools: list[dict[str, Any]] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    api_key: str | None = None,
    shell: bool = False,
    inherit_stdio: bool = False,
    mem_mib: int | None = None,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
    thinking: str | None = None,
    system_prompt: str | None = None,
    system_prompt_set: bool = False,
    codemode: str = "off",
    env_type: str | None = None,
    session_id: str | None = None,
    web_search: bool = False,
    intercept_hosts: tuple[str, ...] = (),
    egress_hooks: EgressHooks | None = None,
) -> StartedMicrovm:
    require_microvm(settings)
    firecracker, jailer = microvm_binaries()
    ip_bin, iptables_bin, tc_bin = microvm_net_binaries()
    selected = image if image is not None else settings.sandbox_default_image
    kernel, rootfs = microvm_images(settings, image=selected)
    resolved = resolve_spawn_image(settings, selected, rootfs)
    guest_mem = mem_mib if mem_mib is not None else settings.microvm_mem_mib
    vm_id = str(uuid.uuid4())
    net = tap_net(vm_id)
    uid = os.getuid()
    gid = os.getgid()
    work = Path(tempfile.mkdtemp(prefix="apipi-microvm-"))
    chroot_dir = work / Path(firecracker).name / vm_id / "root"
    chroot_dir.mkdir(parents=True)
    guest_skills, extra_dirs = guest_skill_dirs(cwd, skill_dirs)
    browser_skills: list[str] = []
    if selected == "browser":
        from apipi.worker.pi.builtin_skills import browser_skill_dir

        arc = ".apipi/skills/browser"
        used = {name for _, name in extra_dirs}
        if arc in used:
            arc = ".apipi/skills/browser-builtin"
        extra_dirs = [*(extra_dirs or []), (browser_skill_dir(), arc)]
        browser_skills = [f"{GUEST_WORKSPACE}/{arc}"]
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
        log_sandbox_boot_failed(exc, vm_id=vm_id)
        raise ConfigError(exc.message) from exc
    stdio_in = None if inherit_stdio else asyncio.subprocess.DEVNULL
    stdio_out = None if inherit_stdio else asyncio.subprocess.PIPE
    console_tasks: list[asyncio.Task[None]] = []
    log.info(
        "boot",
        extra={"vm_id": vm_id, "tap": net.name, "shell": shell},
    )

    egress: EgressGateway | None = None

    def cleanup() -> None:
        for task in console_tasks:
            task.cancel()
        if egress is not None:
            egress.close()
        teardown_tap(
            net,
            ip=ip_bin,
            iptables=iptables_bin,
            tc=tc_bin,
            mode=tap.mode,
        )
        shutil.rmtree(work, ignore_errors=True)

    broker = None
    try:
        from apipi.worker.pi.broker import start_broker
        from apipi.worker.pi.model_host import models_json_for_base_url
        from apipi.worker.pi.settings_json import (
            apply_pi_agent_files,
            merged_settings,
            process_system_prompt,
            settings_json_text,
        )

        if tap.mode != "disabled":
            egress = await start_gateway(
                settings,
                host=net.host_ip,
                mode=tap.mode,
                allowed_hosts=tap.hosts,
                session_id=session_id,
                intercept_hosts=intercept_hosts,
                hooks=egress_hooks,
                dns_upstreams=tuple((dns, 53) for dns in GUEST_DNS),
            )
        broker = await start_broker(
            settings,
            api_key=api_key,
            mcp_http=mcp_http,
            host="0.0.0.0",
            port=0,
            public_host=net.host_ip,
        )
        setup_tap(
            net,
            ip=ip_bin,
            iptables=iptables_bin,
            uid=uid,
            gid=gid,
            ports=tap_ports(broker.port, egress),
            tc=tc_bin,
            mode=tap.mode,
            egress_mbit=settings.microvm_egress_mbit,
        )
        _link_or_copy(Path(kernel), chroot_dir / "vmlinux")
        _link_or_copy(Path(rootfs), chroot_dir / "rootfs.ext4")
        if cwd:
            pi_session_file(Path(cwd)).parent.mkdir(parents=True, exist_ok=True)
        level = thinking if thinking is not None else settings.pi_thinking
        prompt = system_prompt if system_prompt_set else process_system_prompt(settings)
        code = codemode if codemode in ("on", "only") else "off"
        if code != "off" and not tools:
            code = "off"
        agent_dir = Path(cwd) / ".pi" / "agent" if cwd else None
        if agent_dir is not None:
            pi_settings = apply_pi_agent_files(
                agent_dir,
                settings,
                thinking=level,
                system_prompt=prompt,
                env_type=env_type,
                codemode=code,
            )
        else:
            pi_settings = merged_settings(settings, thinking=level, codemode=code)
        system_md = None
        if prompt:
            body = prompt if prompt.endswith("\n") else prompt + "\n"
            system_md = body.encode()
        write_workspace_image(
            chroot_dir / "workspace.tar",
            cwd=cwd,
            env=guest_env(
                settings,
                mcp_http,
                api_key=api_key,
                broker=broker,
                extra_env=extra_env,
                web_search=web_search,
            ),
            pi_args=pi_command_args(
                settings,
                tools=tools,
                mcp_http=mcp_http,
                function_tools=function_tools,
                skill_dirs=guest_skills,
                extra_skill_dirs=browser_skills,
                model=model,
                instructions=instructions,
                session_file=PI_SESSION_REL if cwd else None,
                extension=guest_extensions(web_search),
                thinking=level,
                codemode=code,
                env_type=env_type,
                session_id=session_id,
                web_search=web_search,
            ),
            net=net,
            extra_dirs=extra_dirs,
            shell=shell,
            models_json=models_json_for_base_url(
                settings, broker.openai_base_url, thinking=level, model=model
            ),
            settings_json=settings_json_text(pi_settings).encode(),
            system_md=system_md,
            web_search=web_search,
            egress_ca=egress.ca_pem if egress is not None else None,
        )
        config = microvm_config(
            kernel="vmlinux",
            rootfs="rootfs.ext4",
            workspace="workspace.tar",
            vsock=VSOCK_UDS,
            cid=guest_cid(vm_id),
            net=net,
            mem_mib=guest_mem,
            vcpus=guest_vcpus(settings, guest_mem, image=selected),
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
        log_sandbox_boot_failed(exc, vm_id=vm_id)
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
    except BaseException as exc:
        cleanup()
        if broker is not None:
            with contextlib.suppress(Exception):
                await broker.stop()
        log_sandbox_boot_failed(exc, vm_id=vm_id)
        raise
    pid = process.pid
    if pid is None:
        process.kill()
        await process.wait()
        if broker is not None:
            await broker.stop()
        cleanup()
        log_sandbox_boot_failed(ConfigError("microvm cannot start jailer"), vm_id=vm_id)
        raise ConfigError("microvm cannot start jailer")
    log.info("jailer", extra={"vm_id": vm_id, "pid": pid})
    if not inherit_stdio:
        console_tasks.append(asyncio.create_task(_log_console(process.stdout)))
        console_tasks.append(asyncio.create_task(_log_console(process.stderr)))
    return StartedMicrovm(process, chroot_dir, cleanup, broker, resolved, egress)


async def spawn_microvm_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    function_tools: list[dict[str, Any]] | None = None,
    skill_dirs: list[str] | None = None,
    model: str | None = None,
    instructions: str | None = None,
    api_key: str | None = None,
    mem_mib: int | None = None,
    image: str | None = None,
    extra_env: dict[str, str] | None = None,
    thinking: str | None = None,
    system_prompt: str | None = None,
    system_prompt_set: bool = False,
    codemode: str = "off",
    env_type: str | None = None,
    session_id: str | None = None,
    web_search: bool = False,
    intercept_hosts: tuple[str, ...] = (),
    egress_hooks: EgressHooks | None = None,
) -> PiProc:
    started = await start_microvm(
        settings,
        cwd=cwd,
        tools=tools,
        mcp_http=mcp_http,
        function_tools=function_tools,
        skill_dirs=skill_dirs,
        model=model,
        instructions=instructions,
        api_key=api_key,
        mem_mib=mem_mib,
        image=image,
        extra_env=extra_env,
        thinking=thinking,
        system_prompt=system_prompt,
        system_prompt_set=system_prompt_set,
        codemode=codemode,
        env_type=env_type,
        session_id=session_id,
        web_search=web_search,
        intercept_hosts=intercept_hosts,
        egress_hooks=egress_hooks,
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
        log_sandbox_boot_failed(exc, vm_id=started.chroot_dir.parent.name)
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

    async def pull_metrics() -> bytes:
        return await _pull(VSOCK_METRICS_PORT)

    async def push_files(files: list[tuple[str, bytes]]) -> None:
        await push_workspace_files(started.vsock, files, process=process)

    return PiProc(
        process,
        stdin=writer,
        stdout=reader,
        on_stop=started.cleanup,
        broker=started.broker,
        pull_artifacts=pull_artifacts,
        pull_workspace=pull_workspace,
        pull_session=pull_session,
        pull_metrics=pull_metrics,
        push_files=push_files,
        vm_id=started.chroot_dir.parent.name,
        image=started.image,
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
