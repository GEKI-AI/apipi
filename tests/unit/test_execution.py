import uuid
from typing import cast

import pytest

from apipi.config import Settings
from apipi.env.hub import EnvironmentHub
from apipi.gateway import create_app
from apipi.gateway.errors import ApiError
from apipi.pi.isolation import load_isolation
from apipi.pi.pool import PiPool
from apipi.pi.proc import PiProc
from apipi.services.runtime import EventHub, FakeHarness
from apipi.store.engine import Store
from apipi.worker.execution import LocalExecution


class _Alive:
    alive = True


def test_create_app_sets_local_execution(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    execution = app.state.execution
    assert isinstance(execution, LocalExecution)
    assert execution.harness is harness
    assert execution.pool is app.state.pi_pool
    assert execution.isolation is app.state.isolation
    assert execution.stdio_on_host is app.state.isolation.stdio_on_host


def test_local_execution_capacity_uses_pool(settings: Settings) -> None:
    pool = PiPool(
        Settings(
            database_url=settings.database_url,
            run_mode="none",
            max_sessions=1,
        )
    )
    execution = LocalExecution(
        pool.settings,
        pool=pool,
        harness=FakeHarness(),
        isolation=load_isolation("none"),
        hub=EventHub(),
        env_hub=EnvironmentHub(),
    )
    first = uuid.uuid4()
    second = uuid.uuid4()
    tenant_id = uuid.uuid4()
    assert execution.capacity_code(first, tenant_id) is None
    pool._procs[first] = cast(PiProc, _Alive())
    assert execution.capacity_code(first, tenant_id) is None
    assert execution.capacity_code(second, tenant_id) == "capacity"


async def test_local_execution_cancel_idle_errors(settings: Settings) -> None:
    execution = LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=FakeHarness(),
        isolation=load_isolation("none"),
        hub=EventHub(),
        env_hub=EnvironmentHub(),
    )
    with pytest.raises(ApiError, match="not in_progress"):
        await execution.cancel(uuid.uuid4(), status="idle")


async def test_local_execution_probe_none(settings: Settings) -> None:
    execution = LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=FakeHarness(),
        isolation=load_isolation("none"),
        hub=EventHub(),
        env_hub=EnvironmentHub(),
    )
    execution.require()
    await execution.probe()
