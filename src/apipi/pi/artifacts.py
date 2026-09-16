import asyncio
import io
import mimetypes
import shutil
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.blobs import ArtifactBlobs, blob_store
from apipi.config import ConfigError, DiskLimitError, Settings
from apipi.env.hub import EnvDisconnected, EnvironmentHub
from apipi.pi.dirs import pi_session_file, sessions_root
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.skills import copy_capability_directories
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import (
    create_artifact,
    get_session,
    get_session_by_id,
    list_artifacts,
)

WORKSPACE_ARTIFACTS = "artifacts"
WORKSPACE_OUTPUTS = "outputs"
PUBLISH_DIRS = (WORKSPACE_ARTIFACTS, WORKSPACE_OUTPUTS)


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


def wipe_workspace(workspace: Path) -> None:
    if workspace.is_dir():
        shutil.rmtree(workspace)


async def wipe_artifact_store(
    blobs: ArtifactBlobs,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
) -> None:
    await blobs.delete_session(tenant_id, key_id, session_id)


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


async def _persist_files(
    db: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    files: list[tuple[str, bytes]],
    *,
    turn_id: uuid.UUID | None = None,
    key_id: str = "",
    blobs: ArtifactBlobs | None = None,
) -> None:
    store = blobs if blobs is not None else blob_store(settings)
    existing = await list_artifacts(db, tenant_id, session_id)
    latest: dict[str, bytes] = {}
    if existing:
        for artifact in existing:
            user = artifact.key_id or key_id
            data = await store.get(tenant_id, user, session_id, artifact.id)
            if data is not None:
                latest[artifact.path] = data
    to_write: list[tuple[str, bytes]] = []
    incoming = 0
    for rel, data in files:
        if latest.get(rel) == data:
            continue
        to_write.append((rel, data))
        incoming += len(data)
    used = await store.used_bytes(tenant_id, key_id, session_id)
    row = await get_session_by_id(db, session_id)
    cache = row.pi_session_bytes if row is not None else 0
    if to_write and used - cache + incoming > settings.max_artifact_bytes:
        raise DiskLimitError("Artifact store too large", code="artifact_too_large")
    for rel, data in to_write:
        artifact = await create_artifact(
            db,
            tenant_id,
            session_id,
            path=rel,
            content_type=_content_type(rel),
            turn_id=turn_id,
            key_id=key_id,
            byte_size=len(data),
        )
        await store.put(tenant_id, key_id, session_id, artifact.id, data)
        latest[rel] = data


async def _harvest_self_hosted(
    env_hub: EnvironmentHub, env_id: uuid.UUID
) -> list[tuple[str, bytes]]:
    files: list[tuple[str, bytes]] = []
    for folder in PUBLISH_DIRS:
        try:
            listed = await env_hub.call(env_id, "list", path=folder)
        except (EnvDisconnected, TimeoutError):
            continue
        if not listed.get("ok"):
            continue
        names = listed.get("names")
        if not isinstance(names, list):
            continue
        for raw in names:
            if not isinstance(raw, str) or not _publish_path(raw):
                continue
            try:
                result = await env_hub.call(env_id, "read", path=raw)
            except (EnvDisconnected, TimeoutError):
                continue
            if not result.get("ok"):
                continue
            content = result.get("content")
            if not isinstance(content, str):
                continue
            files.append((raw, content.encode()))
    return files


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


async def harvest_session(
    db: AsyncSession,
    settings: Settings,
    session_id: uuid.UUID,
    proc: PiProc | None,
    env_hub: EnvironmentHub | None = None,
    *,
    turn_id: uuid.UUID | None = None,
    sync_workspace: bool = False,
    blobs: ArtifactBlobs | None = None,
) -> tuple[SessionRow | None, DiskLimitError | None]:
    row = await get_session_by_id(db, session_id)
    if row is None:
        return None, None
    env_type = row.environment.get("type")
    files: list[tuple[str, bytes]] = []
    workspace_error: DiskLimitError | None = None
    if env_type == "self_hosted" and env_hub is not None:
        env_id_raw = row.environment.get("id")
        if isinstance(env_id_raw, str):
            files = await _harvest_self_hosted(env_hub, uuid.UUID(env_id_raw))
    elif env_type == "openai_hosted":
        directory = row.environment.get("directory")
        dest = Path(directory) if isinstance(directory, str) and directory else None
        files, workspace_error = await _hosted_files(
            proc,
            dest,
            sync_workspace=sync_workspace,
            max_workspace_bytes=settings.max_workspace_bytes,
        )
    persist_error: DiskLimitError | None = None
    if files:
        try:
            await _persist_files(
                db,
                settings,
                row.tenant_id,
                row.id,
                files,
                turn_id=turn_id,
                key_id=row.key_id,
                blobs=blobs,
            )
        except DiskLimitError as exc:
            persist_error = exc
    await persist_pi_session(
        db,
        settings,
        row,
        proc,
        dest=_hosted_dest(row),
        blobs=blobs,
    )
    return row, persist_error or workspace_error


def _hosted_dest(row: SessionRow) -> Path | None:
    if row.environment.get("type") != "openai_hosted":
        return None
    directory = row.environment.get("directory")
    if not isinstance(directory, str) or directory == "":
        return None
    return Path(directory)


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


async def persist_pi_session(
    db: AsyncSession,
    settings: Settings,
    row: SessionRow,
    proc: PiProc | None,
    dest: Path | None,
    *,
    blobs: ArtifactBlobs | None = None,
) -> None:
    data = await read_pi_session_bytes(proc, dest)
    if not data:
        return
    store = blobs if blobs is not None else blob_store(settings)
    blob_id = row.pi_session_id if row.pi_session_id is not None else uuid.uuid4()
    await store.put(row.tenant_id, row.key_id, row.id, blob_id, data)
    row.pi_session_id = blob_id
    row.pi_session_bytes = len(data)
    await db.flush()


async def restore_pi_session(
    settings: Settings,
    row: SessionRow,
    dest: Path,
    *,
    blobs: ArtifactBlobs | None = None,
) -> None:
    if row.pi_session_id is None:
        return
    store = blobs if blobs is not None else blob_store(settings)
    data = await store.get(row.tenant_id, row.key_id, row.id, row.pi_session_id)
    if not data:
        return
    path = pi_session_file(dest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def reap_workspaces(
    settings: Settings,
    store: Store,
    pool: PiPool,
    *,
    now: datetime | None = None,
) -> None:
    current = _utc(now or utc_now())
    ttl = settings.workspace_ttl
    root = sessions_root(settings)
    for tenant_dir in root.iterdir():
        if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
            continue
        try:
            tenant_id = uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        for session_dir in tenant_dir.iterdir():
            if not session_dir.is_dir():
                continue
            try:
                session_id = uuid.UUID(session_dir.name)
            except ValueError:
                continue
            if pool.alive(session_id):
                continue
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
            if row is None:
                wipe_workspace(session_dir)
                continue
            env_type = row.environment.get("type")
            if env_type != "openai_hosted":
                continue
            if ttl is None:
                continue
            if current - _utc(row.updated_at) >= ttl:
                wipe_workspace(session_dir)


async def reap_workspace_loop(settings: Settings, store: Store, pool: PiPool) -> None:
    ttl = settings.workspace_ttl
    seconds = ttl.total_seconds() if ttl is not None else 15.0
    interval = min(1.0, max(0.02, seconds / 5))
    while True:
        await asyncio.sleep(interval)
        await reap_workspaces(settings, store, pool)
