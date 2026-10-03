import asyncio
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.api.test_split_robustness import _settings, _status_envelope
from tests.api.test_workers import _leased_session
from tests.support.fake_worker import FakeWorker

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store
from apipi.store.repo import get_session
from apipi.workerhub import serve as serve_module


async def test_a_release_waits_for_the_envelopes_buffered_before_it(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    real = serve_module.flush_batch
    order: list[str] = []

    async def stuck(*args: Any, **kwargs: Any) -> Any:
        await gate.wait()
        order.append("flush")
        return await real(*args, **kwargs)

    monkeypatch.setattr(serve_module, "flush_batch", stuck)
    app = create_app(_settings(settings), store=store)
    hub = app.state.workers
    real_release = hub.release

    async def release(*args: Any, **kwargs: Any) -> Any:
        order.append("release")
        return await real_release(*args, **kwargs)

    monkeypatch.setattr(hub, "release", release)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        tenant_id, session_id, command = await _leased_session(
            app, client, store, worker
        )
        await worker.send_json(_status_envelope(session_id, 1))
        await worker.send_json(
            {
                "type": "lease.release",
                "session_id": str(session_id),
                "lease_id": command["lease_id"],
            }
        )
        await asyncio.sleep(0.1)
        assert order == []
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            assert row is not None and row.lease_id is not None
        gate.set()
        for _ in range(100):
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
                assert row is not None
                if row.lease_id is None:
                    break
            await asyncio.sleep(0.01)
        assert row.lease_id is None
        assert order == ["flush", "release"]
        await worker.close()


async def test_a_full_delta_lane_drops_deltas_and_keeps_the_socket_open(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(serve_module, "DELTA_QUEUE_LIMIT", 2)
    gate = asyncio.Event()

    async def stuck(*_args: Any, **_kwargs: Any) -> bool:
        await gate.wait()
        return False

    app = create_app(_settings(settings), store=store)
    monkeypatch.setattr(app.state.workers, "handle_delta", stuck)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        worker = FakeWorker(app, worker_secret)
        _tenant, session_id, _command = await _leased_session(
            app, client, store, worker
        )
        turn_id = uuid.uuid4()
        for seq in range(1, 9):
            await worker.send_json(
                {
                    "v": 2,
                    "session_id": str(session_id),
                    "turn_id": str(turn_id),
                    "seq": seq,
                    "type": "delta.text",
                    "payload": {"turn_id": str(turn_id), "text": "x"},
                }
            )
        for _ in range(100):
            body = app.state.metrics.scrape().decode()
            if 'event="delta.queue_full"' in body:
                break
            await asyncio.sleep(0.01)
        assert 'event="delta.queue_full"' in body
        conn = app.state.workers.get(uuid.UUID(str(worker.worker_id)))
        assert conn is not None and conn.disconnect_reason is None
        gate.set()
        await worker.close()


async def test_the_websocket_frame_cap_exceeds_the_envelope_cap() -> None:
    from apipi.protocol import MAX_MESSAGE_BYTES

    assert serve_module.WS_MAX_SIZE > 2 * MAX_MESSAGE_BYTES
