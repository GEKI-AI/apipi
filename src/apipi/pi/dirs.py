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
