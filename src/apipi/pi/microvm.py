import asyncio
import contextlib
import io
import json
import os
import shlex
import shutil
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from apipi.config import ConfigError, Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.proc import PiProc, pi_command_args, pi_env

VSOCK_PORT = 52
VSOCK_UDS = "vsock.sock"
MEM_MIB = 512
VCPU_COUNT = 1
CONNECT_TIMEOUT = 30.0
BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off init=/sbin/apipi-guest"


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


def microvm_images(settings: Settings | None = None) -> tuple[str, str]:
    kernel = None
    rootfs = None
    if settings is not None:
        kernel = settings.microvm_kernel
        rootfs = settings.microvm_rootfs
    else:
        kernel = os.environ.get("APIPI_MICROVM_KERNEL")
        rootfs = os.environ.get("APIPI_MICROVM_ROOTFS")
    if not kernel or not Path(kernel).is_file():
        raise ConfigError("APIPI_RUN_MODE=microvm requires APIPI_MICROVM_KERNEL")
    if not rootfs or not Path(rootfs).is_file():
        raise ConfigError("APIPI_RUN_MODE=microvm requires APIPI_MICROVM_ROOTFS")
    return kernel, rootfs


def require_microvm(settings: Settings | None = None) -> None:
    if not kvm_available():
        raise ConfigError("APIPI_RUN_MODE=microvm requires /dev/kvm")
    microvm_binaries()
    microvm_images(settings)


def guest_cid(vm_id: str) -> int:
    return uuid.UUID(vm_id).int % (2**32 - 3) + 3


def guest_env(
    settings: Settings,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
) -> dict[str, str]:
    env = pi_env(settings, mcp_http, mcp_stdio)
    return {
        key: value
        for key, value in env.items()
        if key.startswith("OPENAI_") or key.startswith("APIPI_")
    }


def env_file(env: dict[str, str]) -> str:
    lines = [
        f"{key}={shlex.quote(value)}"
        for key, value in env.items()
        if key != "DATABASE_URL"
    ]
    return "\n".join(lines) + ("\n" if lines else "")


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
) -> None:
    guest_py = Path(__file__).with_name("guest.py").read_bytes()
    guest_sh = Path(__file__).with_name("guest.sh").read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        if cwd:
            tar.add(str(Path(cwd).resolve()), arcname=".", recursive=True)
        _add_bytes(tar, ".apipi/env", env_file(env).encode(), mode=0o600)
        _add_bytes(tar, ".apipi/pi-args", json.dumps(pi_args).encode(), mode=0o644)
        _add_bytes(tar, ".apipi/pi-cmd", shlex.join(pi_args).encode(), mode=0o644)
        _add_bytes(tar, ".apipi/guest.py", guest_py, mode=0o755)
        _add_bytes(tar, ".apipi/guest.sh", guest_sh, mode=0o755)
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
) -> dict[str, object]:
    return {
        "boot-source": {
            "kernel_image_path": kernel,
            "boot_args": BOOT_ARGS,
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
            "vcpu_count": VCPU_COUNT,
            "mem_size_mib": MEM_MIB,
        },
        "vsock": {
            "guest_cid": cid,
            "uds_path": vsock,
        },
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


async def spawn_microvm_pi(
    settings: Settings,
    *,
    cwd: str | None,
    tools: bool,
    mcp_http: list[McpHttpServer] | None = None,
    mcp_stdio: list[McpStdioServer] | None = None,
    skill_dirs: list[str] | None = None,
) -> PiProc:
    require_microvm(settings)
    firecracker, jailer = microvm_binaries()
    kernel, rootfs = microvm_images(settings)
    vm_id = str(uuid.uuid4())
    work = Path(tempfile.mkdtemp(prefix="apipi-microvm-"))
    chroot_dir = work / Path(firecracker).name / vm_id / "root"
    chroot_dir.mkdir(parents=True)
    try:
        _link_or_copy(Path(kernel), chroot_dir / "vmlinux")
        _link_or_copy(Path(rootfs), chroot_dir / "rootfs.ext4")
        write_workspace_image(
            chroot_dir / "workspace.tar",
            cwd=cwd,
            env=guest_env(settings, mcp_http, mcp_stdio),
            pi_args=pi_command_args(
                settings,
                tools=tools,
                mcp_http=mcp_http,
                mcp_stdio=mcp_stdio,
                skill_dirs=skill_dirs,
            ),
        )
        config = microvm_config(
            kernel="vmlinux",
            rootfs="rootfs.ext4",
            workspace="workspace.tar",
            vsock=VSOCK_UDS,
            cid=guest_cid(vm_id),
        )
        (chroot_dir / "config.json").write_text(json.dumps(config))
        argv = jailer_argv(
            jailer=jailer,
            firecracker=firecracker,
            vm_id=vm_id,
            uid=os.getuid(),
            gid=os.getgid(),
            chroot_base=str(work),
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc
    pid = process.pid
    if pid is None:
        process.kill()
        await process.wait()
        shutil.rmtree(work, ignore_errors=True)
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start")
    try:
        reader, writer = await connect_vsock(
            chroot_dir / VSOCK_UDS,
            VSOCK_PORT,
            process=process,
        )
    except (ConfigError, OSError) as exc:
        process.kill()
        await process.wait()
        shutil.rmtree(work, ignore_errors=True)
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError("APIPI_RUN_MODE=microvm cannot start") from exc
    return PiProc(
        process,
        stdin=writer,
        stdout=reader,
        on_stop=lambda: shutil.rmtree(work, ignore_errors=True),
    )
