import io
import mimetypes
import tarfile
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apipi.common.background import run_loop
from apipi.common.dirs import pi_session_file, sessions_root, wipe_workspace
from apipi.common.skills import copy_capability_directories
from apipi.config import ConfigError, DiskLimitError, Settings
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc

WORKSPACE_OUTPUTS = "outputs"
PUBLISH_DIRS = (WORKSPACE_OUTPUTS,)


def _content_type(path: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "application/octet-stream"


def _publish_path(name: str) -> bool:
    return any(
        name == folder or name.startswith(f"{folder}/") for folder in PUBLISH_DIRS
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def unpack_artifact_tar(data: bytes) -> list[tuple[str, bytes]]:
    if not data:
        return []
    files: list[tuple[str, bytes]] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for info in tar.getmembers():
            if not info.isfile():
                continue
            name = info.name
            if name.startswith("./"):
                name = name[2:]
            parts = Path(name).parts
            if ".." in parts:
                continue
            if not _publish_path(name):
                continue
            handle = tar.extractfile(info)
            if handle is None:
                continue
            files.append((name, handle.read()))
    return files


def dir_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def unpack_workspace_tar(
    data: bytes, dest: Path, *, max_bytes: int | None = None
) -> None:
    if not data:
        return
    if max_bytes is not None and dir_bytes(dest) > max_bytes:
        raise DiskLimitError("Workspace too large", code="workspace_too_large")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        members: list[tuple[tarfile.TarInfo, str]] = []
        total = 0
        for info in tar.getmembers():
            if not info.isfile():
                continue
            name = info.name
            if name.startswith("./"):
                name = name[2:]
            parts = Path(name).parts
            if not parts or ".." in parts or parts[0] == ".apipi":
                continue
            members.append((info, name))
            total += info.size
        if max_bytes is not None and total > max_bytes:
            raise DiskLimitError("Workspace too large", code="workspace_too_large")
        for info, name in members:
            handle = tar.extractfile(info)
            if handle is None:
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())


def read_workspace_artifacts(workspace: Path) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for folder in PUBLISH_DIRS:
        root = workspace / folder
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(workspace).as_posix()
            files.append((rel, path.read_bytes()))
    return files


def ensure_openai_workspace(environment: dict[str, Any]) -> None:
    if environment.get("type") != "openai_hosted":
        return
    directory = environment.get("directory")
    if not isinstance(directory, str) or directory == "":
        return
    path = Path(directory)
    if path.is_dir() and any(path.iterdir()):
        return
    path.mkdir(parents=True, exist_ok=True)
    caps = environment.get("capability_directories")
    if isinstance(caps, list):
        copy_capability_directories(
            path, [item for item in caps if isinstance(item, str)]
        )


async def _hosted_files(
    proc: PiProc | None,
    dest: Path | None,
    *,
    sync_workspace: bool,
    max_workspace_bytes: int | None = None,
) -> tuple[list[tuple[str, bytes]], DiskLimitError | None]:
    workspace_error: DiskLimitError | None = None
    if (
        sync_workspace
        and proc is not None
        and proc.pull_workspace is not None
        and dest is not None
    ):
        try:
            unpack_workspace_tar(
                await proc.pull_workspace(), dest, max_bytes=max_workspace_bytes
            )
        except DiskLimitError as exc:
            workspace_error = exc
        except (OSError, TimeoutError, ConfigError):
            pass
    files: list[tuple[str, bytes]] = []
    if proc is not None and proc.pull_artifacts is not None:
        try:
            files = unpack_artifact_tar(await proc.pull_artifacts())
        except (OSError, TimeoutError, ConfigError):
            if dest is not None:
                files = read_workspace_artifacts(dest)
    elif dest is not None:
        files = read_workspace_artifacts(dest)
    if (
        dest is not None
        and max_workspace_bytes is not None
        and dir_bytes(dest) > max_workspace_bytes
    ):
        workspace_error = workspace_error or DiskLimitError(
            "Workspace too large", code="workspace_too_large"
        )
    return files, workspace_error


async def read_pi_session_bytes(proc: PiProc | None, dest: Path | None) -> bytes:
    if proc is not None and proc.pull_session is not None:
        try:
            return await proc.pull_session()
        except (OSError, TimeoutError, ConfigError):
            pass
    if dest is None:
        return b""
    path = pi_session_file(dest)
    if not path.is_file():
        return b""
    return path.read_bytes()


async def reap_workspaces(
    settings: Settings,
    pool: PiPool,
    *,
    now: datetime | None = None,
    ttl_overrides: Mapping[str, tuple[float | None, float, str | None]] | None = None,
) -> list[str]:
    current = _utc(now or datetime.now(UTC))
    now_epoch = current.timestamp()
    wiped: list[str] = []
    root = sessions_root(settings)
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
            continue
        try:
            uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        for session_dir in tenant_dir.iterdir():
            if not session_dir.is_dir():
                continue
            try:
                session_id = uuid.UUID(session_dir.name)
            except ValueError:
                continue
            if pool.alive(session_id) or pool.held(session_id):
                continue
            override = ttl_overrides.get(str(session_id)) if ttl_overrides else None
            if override is None:
                continue
            ttl_seconds, last_seen, env_type = override
            if env_type != "openai_hosted":
                continue
            if ttl_seconds is None:
                continue
            if now_epoch - last_seen >= ttl_seconds:
                wipe_workspace(session_dir)
                wiped.append(str(session_id))
    return wiped


async def reap_workspace_loop(
    settings: Settings,
    pool: PiPool,
    *,
    ttl_overrides: Mapping[str, tuple[float | None, float, str | None]] | None = None,
    on_wiped: Callable[[str], None] | None = None,
    metrics: Any | None = None,
) -> None:
    ttl = settings.sandbox_ttl_openai_hosted
    seconds = ttl.total_seconds() if ttl is not None else 15.0
    interval = min(1.0, max(0.02, seconds / 5))

    async def reap_round() -> None:
        wiped = await reap_workspaces(settings, pool, ttl_overrides=ttl_overrides)
        if on_wiped is not None:
            for session_id in wiped:
                on_wiped(session_id)

    await run_loop("workspace_reaper", reap_round, interval=interval, metrics=metrics)
