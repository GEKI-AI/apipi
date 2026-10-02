import uuid
from typing import cast

import pytest

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.errors import ApiError
from apipi.services.runtime import EventHub, FakeHarness
from apipi.store.engine import Store
from apipi.worker.execution import LocalExecution, RemoteExecution
from apipi.worker.outbox import Outbox
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc


class _Alive:
    alive = True


def test_create_app_sets_remote_execution(settings: Settings, store: Store) -> None:
    app = create_app(settings, store=store)
    execution = app.state.execution
    assert isinstance(execution, RemoteExecution)
    assert execution.workers is app.state.workers
    assert not hasattr(app.state, "pi_pool")


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
        hub=EventHub(),
        outbox=Outbox(),
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
        hub=EventHub(),
        outbox=Outbox(),
    )
    with pytest.raises(ApiError, match="not in_progress"):
        await execution.cancel(uuid.uuid4(), status="idle")
