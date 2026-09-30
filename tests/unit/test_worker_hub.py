import logging
import uuid
from unittest.mock import MagicMock

import pytest
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.gateway.metrics import Metrics
from apipi.services.runtime import EventHub
from apipi.store.engine import Store
from apipi.store.repo import create_session, create_tenant, list_events
from apipi.worker.hub import (
    WorkerConnection,
    WorkerHub,
    WorkerImage,
    dispatch_command,
    images_from_message,
)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        microvm_mem_mib=512,
    )


def _conn(*, capacity: int, memory_mb: int, run_mode: str = "chat") -> WorkerConnection:
    return WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=capacity,
        memory_mb=memory_mb,
        run_mode=run_mode,
    )


def test_pick_filters_image_before_capacity() -> None:
    hub = WorkerHub(_settings())
    image = WorkerImage("browser", "1", "abc", "M")
    browser = _conn(capacity=1, memory_mb=4096, run_mode="microvm")
    browser.images = {"browser": image}
    full = _conn(capacity=1, memory_mb=4096, run_mode="microvm")
    full.images = {"browser": image}
    full.leases.add(uuid.uuid4())
    plain = _conn(capacity=8, memory_mb=4096, run_mode="microvm")
    chat = _conn(capacity=8, memory_mb=4096, run_mode="chat")
    hub._conns[browser.worker_id] = browser
    hub._conns[full.worker_id] = full
    hub._conns[plain.worker_id] = plain
    hub._conns[chat.worker_id] = chat
    assert hub.pick(512, run_mode="microvm", image="browser") is browser
    assert hub.has_image("microvm", "missing") is False
    assert hub.pick(512, run_mode="chat") is chat


def test_legacy_worker_without_images_has_default_and_browser() -> None:
    images = images_from_message({"type": "register"}, "microvm")
    assert set(images) == {"default", "browser"}
    arm = images_from_message({"type": "register", "arch": "aarch64"}, "microvm")
    assert set(arm) == {"default"}
    assert images_from_message({"type": "register"}, "chat") == {}
    assert images_from_message({"images": []}, "microvm") == {}


def test_pick_prefers_more_free_ram() -> None:
    hub = WorkerHub(_settings())
    low = _conn(capacity=8, memory_mb=1024)
    high = _conn(capacity=8, memory_mb=4096)
    hub._conns[low.worker_id] = low
    hub._conns[high.worker_id] = high
    assert hub.pick(run_mode="chat") is high


def test_pick_rejects_ram_cap_with_session_slots() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=512)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(run_mode="chat") is None


def test_pick_rejects_session_cap_with_ram() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=1, memory_mb=8192)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(run_mode="chat") is None


def test_pick_uses_lease_mem_for_mixed_sizes() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=2560)
    lease = uuid.uuid4()
    conn.leases.add(lease)
    conn.lease_mem[lease] = 2048
    hub._conns[conn.worker_id] = conn
    assert hub.pick(512, run_mode="chat") is conn
    assert hub.pick(1024, run_mode="chat") is None


def test_pick_tie_break_fewer_leases() -> None:
    hub = WorkerHub(_settings())
    busy = _conn(capacity=8, memory_mb=4096)
    busy.leases.add(uuid.uuid4())
    idle = _conn(capacity=8, memory_mb=3584)
    hub._conns[busy.worker_id] = busy
    hub._conns[idle.worker_id] = idle
    assert hub.pick(run_mode="chat") is idle


def test_observe_labels_workers_by_run_mode() -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="chat")
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="microvm")
    hub._observe()
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_workers", run_mode="chat").endswith(" 1.0")
    assert metric_line(body, "apipi_workers", run_mode="microvm").endswith(" 1.0")
    assert metric_line(body, "apipi_worker_leases", run_mode="chat").endswith(" 0.0")


async def test_dispatch_reports_escaped_turn(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id, model="m1")
        tenant_id = tenant.id
        session_id = session.id
    hub = EventHub()

    class Boom:
        def __init__(self) -> None:
            self.store = store
            self.hub = hub

        async def run_turn(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

    caplog.set_level(logging.ERROR, logger="apipi.worker")
    await dispatch_command(
        Boom(),
        {
            "op": "turn.start",
            "session_id": str(session_id),
            "payload": {"tenant_id": str(tenant_id), "request_id": "req-1"},
        },
    )
    async with store.session() as db:
        events = await list_events(db, tenant_id, session_id)
    assert any(event.type == "agent.session.failed" for event in events)
    assert any(
        event.type == "agent.session.error"
        and isinstance(event.data, dict)
        and event.data.get("code") == "internal"
        and event.data.get("failure_source") == "internal"
        for event in events
    )
    assert any(
        getattr(record, "request_id", None) == "req-1" for record in caplog.records
    )
    assert any(
        getattr(record, "session_id", None) == str(session_id)
        for record in caplog.records
    )
    assert any(
        getattr(record, "tenant_id", None) == str(tenant_id)
        for record in caplog.records
    )


async def test_dispatch_logs_4xx_and_does_not_raise(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        session = await create_session(db, tenant.id, model="m1")
        tenant_id = tenant.id
        session_id = session.id

    class Denied:
        def __init__(self) -> None:
            self.store = store
            self.hub = EventHub()

        async def run_turn(self, *_args: object, **_kwargs: object) -> None:
            raise ApiError(
                "invalid_request",
                "Model host /models is unreachable",
                code="model_host_unreachable",
                status_code=400,
            )

    caplog.set_level(logging.WARNING, logger="apipi.worker")
    await dispatch_command(
        Denied(),
        {
            "op": "turn.start",
            "session_id": str(session_id),
            "payload": {"tenant_id": str(tenant_id), "request_id": "req-2"},
        },
    )
    matched = [
        record
        for record in caplog.records
        if getattr(record, "error_code", None) == "model_host_unreachable"
    ]
    assert matched
    assert matched[-1].levelno == logging.WARNING
    assert matched[-1].__dict__["failure_source"] == "user"
