from pathlib import Path

from apipi.common.dirs import sessions_root
from apipi.config import Settings
from apipi.gateway.auth import tenant_from_key


def hosted_dir(settings: Settings, token: str, session_id: str) -> Path:
    return sessions_root(settings) / str(tenant_from_key(token)) / session_id
