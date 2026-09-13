import uuid
from pathlib import Path

from apipi.config import Settings


def sessions_root(settings: Settings) -> Path:
    if settings.sessions_dir:
        root = Path(settings.sessions_dir)
    else:
        root = Path.cwd() / ".apipi" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_workspace(
    settings: Settings, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Path:
    path = sessions_root(settings) / str(tenant_id) / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def blob_user(key_id: str) -> str:
    raw = key_id.strip()
    if not raw or "/" in raw or ".." in raw:
        return "_"
    return raw


def artifact_blob_dir(
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    key_id: str = "",
) -> Path:
    path = (
        sessions_root(settings)
        / ".artifacts"
        / str(tenant_id)
        / blob_user(key_id)
        / str(session_id)
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def artifact_blob_path(
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    key_id: str = "",
) -> Path:
    return artifact_blob_dir(settings, tenant_id, session_id, key_id) / str(artifact_id)
