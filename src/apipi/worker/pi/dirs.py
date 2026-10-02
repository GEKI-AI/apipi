import uuid
from pathlib import Path

from apipi.config import Settings

PI_SESSION_REL = ".apipi/pi-session.jsonl"


def sessions_root(settings: Settings) -> Path:
    if settings.sessions_dir:
        root = Path(settings.sessions_dir)
    else:
        root = Path.cwd() / ".apipi" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def store_root(settings: Settings) -> Path:
    """Root for local artifact, file, and skill bytes.

    ``APIPI_LOCAL_STORE_DIR``, which defaults to ``.apipi/store``. The
    API and every worker must mount this path at the same location;
    per-worker ``APIPI_SESSIONS_DIR`` values stay separate workspaces.
    """
    local_dir = settings.local_store_dir
    root = (
        Path(local_dir.strip())
        if local_dir and local_dir.strip()
        else sessions_root(settings)
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def session_workspace(
    settings: Settings, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Path:
    path = sessions_root(settings) / str(tenant_id) / str(session_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def pi_session_file(workspace: Path) -> Path:
    return workspace / PI_SESSION_REL


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
        store_root(settings)
        / ".artifacts"
        / str(tenant_id)
        / blob_user(key_id)
        / str(session_id)
    )
    path.mkdir(parents=True, exist_ok=True)
    return path
