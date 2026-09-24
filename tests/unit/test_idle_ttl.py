import time
import uuid
from datetime import timedelta
from typing import cast

import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.idle import normalize_idle_ttl, resolve_idle_ttl
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


def _settings(**updates: object) -> Settings:
    base = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        idle_ttl=timedelta(minutes=15),
        workspace_ttl=timedelta(hours=1),
    )
    if not updates:
        return base
    return base.model_copy(update=updates)


def test_normalize_idle_ttl() -> None:
    assert normalize_idle_ttl(None) is None
    assert normalize_idle_ttl("30m") == "30m"
    assert normalize_idle_ttl("0") == "0"
    assert normalize_idle_ttl("off") == "0"
    with pytest.raises(ApiError):
        normalize_idle_ttl("soon")


def test_resolve_session_over_agent_over_default() -> None:
    settings = _settings()
    assert resolve_idle_ttl(
        settings,
        "none",
        session_idle="30m",
        session_metadata={"apipi.idle_ttl": "5m"},
        agent_idle="1h",
    ) == timedelta(minutes=30)
    assert resolve_idle_ttl(
        settings,
        "none",
        session_idle=None,
        session_metadata={"apipi.idle_ttl": "5m"},
        agent_idle="1h",
    ) == timedelta(minutes=5)
    assert resolve_idle_ttl(
        settings,
        "openai_hosted",
        session_idle=None,
        session_metadata={},
        agent_idle="30m",
    ) == timedelta(minutes=30)
    assert resolve_idle_ttl(
        settings,
        "none",
        session_idle=None,
        session_metadata={},
        agent_idle=None,
    ) == timedelta(minutes=15)
    assert (
        resolve_idle_ttl(
            settings,
            "openai_hosted",
            session_idle="0",
            session_metadata={},
            agent_idle="30m",
        )
        is None
    )


class _Proc:
    alive = True
    vm_id = None

    async def terminate(self) -> None:
        self.alive = False


async def test_pool_reap_uses_stored_ttl() -> None:
    pool = PiPool(_settings())
    short = uuid.uuid4()
    default = uuid.uuid4()
    pool._procs[short] = cast(PiProc, _Proc())
    pool._procs[default] = cast(PiProc, _Proc())
    pool._env_types[short] = "none"
    pool._env_types[default] = "none"
    pool._last[short] = time.monotonic() - 120
    pool._last[default] = time.monotonic() - 120
    pool._ttls[short] = 60.0
    await pool.reap()
    assert short not in pool._procs
    assert default in pool._procs
