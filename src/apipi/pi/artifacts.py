import io
import mimetypes
import shutil
import tarfile
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import Settings
from apipi.env.hub import EnvDisconnected, EnvironmentHub
from apipi.pi.dirs import artifact_blob_path, sessions_root
from apipi.pi.proc import PiProc
from apipi.skills import copy_capability_directories
from apipi.store.models import SessionRow
from apipi.store.repo import create_artifact, get_session_by_id

WORKSPACE_ARTIFACTS = "artifacts"


def _content_type(path: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path)
    if guessed:
        return guessed
    return "application/octet-stream"


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
            if not name.startswith(f"{WORKSPACE_ARTIFACTS}/"):
                continue
            handle = tar.extractfile(info)
            if handle is None:
                continue
            files.append((name, handle.read()))
    return files


def read_workspace_artifacts(workspace: Path) -> list[tuple[str, bytes]]:
    root = workspace / WORKSPACE_ARTIFACTS
    if not root.is_dir():
        return []
    files: list[tuple[str, bytes]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(workspace).as_posix()
        files.append((rel, path.read_bytes()))
    return files


def wipe_workspace(workspace: Path) -> None:
    if workspace.is_dir():
        shutil.rmtree(workspace)


def wipe_artifact_store(
    settings: Settings, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    path = sessions_root(settings) / ".artifacts" / str(tenant_id) / str(session_id)
    if path.is_dir():
        shutil.rmtree(path)


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
) -> None:
    for rel, data in files:
        artifact = await create_artifact(
            db,
            tenant_id,
            session_id,
            path=rel,
            content_type=_content_type(rel),
        )
        dest = artifact_blob_path(settings, tenant_id, session_id, artifact.id)
        dest.write_bytes(data)


async def _harvest_self_hosted(
    env_hub: EnvironmentHub, env_id: uuid.UUID
) -> list[tuple[str, bytes]]:
    try:
        listed = await env_hub.call(env_id, "list", path=WORKSPACE_ARTIFACTS)
    except (EnvDisconnected, TimeoutError):
        return []
    if not listed.get("ok"):
        return []
    names = listed.get("names")
    if not isinstance(names, list):
        return []
    files: list[tuple[str, bytes]] = []
    for raw in names:
        if not isinstance(raw, str) or not raw.startswith(f"{WORKSPACE_ARTIFACTS}/"):
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


async def harvest_session(
    db: AsyncSession,
    settings: Settings,
    session_id: uuid.UUID,
    proc: PiProc | None,
    env_hub: EnvironmentHub | None = None,
) -> SessionRow | None:
    row = await get_session_by_id(db, session_id)
    if row is None:
        return None
    env_type = row.environment.get("type")
    files: list[tuple[str, bytes]] = []
    if env_type == "self_hosted" and env_hub is not None:
        env_id_raw = row.environment.get("id")
        if isinstance(env_id_raw, str):
            files = await _harvest_self_hosted(env_hub, uuid.UUID(env_id_raw))
    elif env_type == "openai_hosted":
        if proc is not None and proc.pull_artifacts is not None:
            data = await proc.pull_artifacts()
            files = unpack_artifact_tar(data)
        else:
            directory = row.environment.get("directory")
            if isinstance(directory, str) and directory:
                files = read_workspace_artifacts(Path(directory))
    if files:
        await _persist_files(db, settings, row.tenant_id, row.id, files)
    if env_type == "openai_hosted":
        directory = row.environment.get("directory")
        if isinstance(directory, str) and directory:
            wipe_workspace(Path(directory))
    return row
