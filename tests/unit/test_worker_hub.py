import logging
import time
import uuid
from unittest.mock import MagicMock

import pytest
from tests.support.prom import metric_line

from apipi.common.errors import ApiError
from apipi.common.event_bus import EventHub
from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.worker.commands import dispatch_command
from apipi.worker.outbox import Outbox
from apipi.worker.sink import OutboxSink
from apipi.workerhub.connection import WorkerConnection, WorkerImage
from apipi.workerhub.heartbeat import images_from_message, observe_heartbeat
from apipi.workerhub.hub import WorkerHub


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        microvm_mem_mib=512,
    )


def _conn(
    *,
    capacity: int,
    memory_mb: int,
    run_mode: str = "none",
    accepts: frozenset[str] | None = None,
) -> WorkerConnection:
    return WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=capacity,
        memory_mb=memory_mb,
        run_mode=run_mode,
        accepts=accepts
        if accepts is not None
        else (
            frozenset({"none", "microvm"})
            if run_mode == "microvm"
            else frozenset({"none"})
        ),
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
    none_only = _conn(capacity=8, memory_mb=16384, run_mode="none")
    hub._conns[browser.worker_id] = browser
    hub._conns[full.worker_id] = full
    hub._conns[plain.worker_id] = plain
    hub._conns[none_only.worker_id] = none_only
    assert hub.pick(512, kind="microvm", image="browser") is browser
    assert hub.has_image("microvm", "missing") is False
    assert hub.pick(512, kind="none") is none_only


def test_a_microvm_worker_that_omits_images_has_default_and_browser() -> None:
    images = images_from_message({"type": "register"}, "microvm")
    assert set(images) == {"default", "browser"}
    arm = images_from_message({"type": "register", "arch": "aarch64"}, "microvm")
    assert set(arm) == {"default"}
    assert images_from_message({"type": "register"}, "none") == {}
    assert images_from_message({"images": []}, "microvm") == {}


@pytest.mark.parametrize(
    ("pools", "none", "microvm"),
    [
        ([{"none"}], 0, None),
        ([{"microvm"}], None, 0),
        ([{"none", "microvm"}], 0, 0),
        ([{"none"}, {"microvm"}], 0, 1),
    ],
)
def test_pick_places_each_kind_only_on_a_worker_that_accepts_it(
    pools: list[set[str]], none: int | None, microvm: int | None
) -> None:
    hub = WorkerHub(_settings())
    conns = [
        _conn(capacity=8, memory_mb=4096 * (index + 1), accepts=frozenset(accepts))
        for index, accepts in enumerate(pools)
    ]
    for conn in conns:
        hub._conns[conn.worker_id] = conn
    for kind, want in (("none", none), ("microvm", microvm)):
        assert hub.pick(kind=kind) is (None if want is None else conns[want])


def test_pick_prefers_more_free_ram() -> None:
    hub = WorkerHub(_settings())
    low = _conn(capacity=8, memory_mb=1024)
    high = _conn(capacity=8, memory_mb=4096)
    hub._conns[low.worker_id] = low
    hub._conns[high.worker_id] = high
    assert hub.pick(kind="none") is high


def test_pick_rejects_ram_cap_with_session_slots() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=512)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(kind="none") is None


def test_pick_rejects_session_cap_with_ram() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=1, memory_mb=8192)
    conn.leases.add(uuid.uuid4())
    hub._conns[conn.worker_id] = conn
    assert hub.pick(kind="none") is None


def test_pick_uses_lease_mem_for_mixed_sizes() -> None:
    hub = WorkerHub(_settings())
    conn = _conn(capacity=8, memory_mb=2560)
    lease = uuid.uuid4()
    conn.leases.add(lease)
    conn.lease_mem[lease] = 2048
    hub._conns[conn.worker_id] = conn
    assert hub.pick(512, kind="none") is conn
    assert hub.pick(1024, kind="none") is None


def test_pick_tie_break_fewer_leases() -> None:
    hub = WorkerHub(_settings())
    busy = _conn(capacity=8, memory_mb=4096)
    busy.leases.add(uuid.uuid4())
    idle = _conn(capacity=8, memory_mb=3584)
    hub._conns[busy.worker_id] = busy
    hub._conns[idle.worker_id] = idle
    assert hub.pick(kind="none") is idle


def test_observe_labels_workers_by_run_mode() -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="none")
    hub._conns[uuid.uuid4()] = _conn(capacity=1, memory_mb=512, run_mode="microvm")
    hub._observe()
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_workers", run_mode="none").endswith(" 1.0")
    assert metric_line(body, "apipi_workers", run_mode="microvm").endswith(" 1.0")
    assert metric_line(body, "apipi_worker_leases", run_mode="none").endswith(" 0.0")


def test_heartbeat_gap_is_measured_and_late_gaps_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = Metrics()
    hub = WorkerHub(_settings(), metrics=metrics)
    conn = _conn(capacity=1, memory_mb=512)
    conn.connected_at = time.monotonic() - 20
    with caplog.at_level("WARNING", logger="apipi.worker"):
        observe_heartbeat(hub, conn)
        observe_heartbeat(hub, conn)
    late = [
        record
        for record in caplog.records
        if getattr(record, "event", None) == "worker.heartbeat.late"
    ]
    assert len(late) == 1
    assert late[0].__dict__["source"] == "api"
    assert late[0].__dict__["gap_seconds"] >= 20
    body = metrics.scrape().decode()
    assert "apipi_worker_heartbeat_gap_seconds_count 2.0" in body


class _SinkExecution:
    def __init__(self) -> None:
        self.hub = EventHub()
        self.outbox = Outbox()

    def sink_for(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> OutboxSink:
        return OutboxSink(self.outbox, tenant_id, session_id)


async def test_dispatch_reports_escaped_turn(caplog: pytest.LogCaptureFixture) -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()

    class Boom(_SinkExecution):
        async def run_turn(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("boom")

    caplog.set_level(logging.ERROR, logger="apipi.worker")
    execution = Boom()
    await dispatch_command(
        execution,
        {
            "op": "turn.start",
            "session_id": str(session_id),
            "payload": {"tenant_id": str(tenant_id), "request_id": "req-1"},
        },
    )
    events = [
        item["payload"]
        for item in execution.outbox.pending(session_id)
        if item["type"] == "event"
    ]
    assert any(event["type"] == "agent.session.failed" for event in events)
    assert any(
        event["type"] == "agent.session.error"
        and event["data"].get("code") == "internal"
        and event["data"].get("failure_source") == "internal"
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()

    class Denied(_SinkExecution):
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


async def test_a_release_ends_only_when_every_handler_of_it_ended() -> None:
    hub = WorkerHub(_settings())
    hub.release_wait = 0.0
    session_id, lease_id = uuid.uuid4(), uuid.uuid4()
    first = hub.begin_release(session_id, lease_id)
    second = hub.begin_release(session_id, lease_id)
    assert second is first
    hub.end_release(first)
    assert not first.done.is_set()
    assert await hub.settle_release(session_id, lease_id) is False
    hub.end_release(second)
    assert first.done.is_set()
    assert await hub.settle_release(session_id) is False
    again = hub.begin_release(session_id, lease_id)
    assert again is not first
    hub.end_release(first)
    assert not again.done.is_set()
    hub.end_release(again)
    assert again.done.is_set()
