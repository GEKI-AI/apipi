import logging
import uuid

import pytest
from tests.support.fake_worker import FakeWorker
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _metrics_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        metrics=True,
    )


async def test_v1_register_is_rejected(
    settings: Settings,
    store: Store,
    worker_secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(_metrics_settings(settings), store=store, harness=FakeHarness())
    caplog.set_level(logging.WARNING, logger="apipi.worker")
    worker = FakeWorker(app, worker_secret)
    await worker.connect(protocol=None)
    assert worker.hello is not None
    assert worker.hello.get("ok") is False
    assert worker.hello.get("error") == "unsupported_protocol"
    closed = await worker.wait_close()
    assert closed["code"] == 1008
    assert closed["reason"] == "unsupported_protocol"
    await worker.close()
    assert "worker protocol rejected" in caplog.text
    assert any(
        getattr(record, "reason", None) == "unsupported_protocol"
        for record in caplog.records
    )
    assert app.state.metrics is not None
    body = app.state.metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_protocol_total", event="unsupported_protocol"
    ).endswith(" 1.0")


async def test_first_message_must_be_register(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, worker_secret)
    await worker.ws.connect()
    await worker.send_json({"type": "heartbeat"})
    hello = await worker.receive_json()
    assert hello.get("ok") is False
    assert hello.get("error") == "register required"
    await worker.close()


async def test_invalid_register_is_rejected(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, worker_secret)
    await worker.connect(run_mode="")
    assert worker.hello is not None
    assert worker.hello.get("ok") is False
    assert worker.hello.get("error") == "invalid register"
    await worker.close()


async def test_hello_reply_carries_protocol_and_sessions(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=2)
    assert hello["ok"] is True
    assert hello["type"] == "hello"
    assert hello["protocol"] == 2
    assert hello["sessions"] == {}
    assert uuid.UUID(str(hello["worker_id"]))
    assert hello["generation"] == 1
    await worker.close()
