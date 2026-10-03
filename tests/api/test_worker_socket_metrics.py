import asyncio
import logging

import pytest
from fastapi import FastAPI
from tests.support.fake_worker import FakeWorker
from tests.support.logs import field
from tests.support.prom import metric_line
from tests.support.split_worker import api_settings_for

from apipi.common.logutil import ContextFilter
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store


async def _until(app: FastAPI, needle: str) -> str:
    body = ""
    for _ in range(100):
        body = app.state.metrics.scrape().decode()
        if needle in body:
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(f"{needle} missing in {body}")


async def test_socket_metrics_and_logs(
    settings: Settings,
    store: Store,
    worker_secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(
        api_settings_for(settings.model_copy(update={"metrics": True})), store=store
    )
    caplog.set_level(logging.INFO, logger="apipi.worker")
    caplog.handler.addFilter(ContextFilter())
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    connection_id = hello["connection_id"]
    assert connection_id
    await worker.send_json({"type": "heartbeat"})
    body = await _until(app, 'apipi_worker_handle_seconds_count{type="heartbeat"} 1.0')
    assert metric_line(body, "apipi_worker_connects_total", result="ok").endswith(
        " 1.0"
    )
    assert metric_line(body, "apipi_worker_connections", run_mode="none").endswith(
        " 1.0"
    )
    assert metric_line(
        body, "apipi_worker_messages_total", direction="in", type="heartbeat"
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_worker_messages_total", direction="out", type="hello"
    ).endswith(" 1.0")
    assert metric_line(
        body,
        "apipi_worker_info",
        worker_id=str(worker.worker_id),
        protocol="2",
        version="unknown",
        run_mode="none",
    ).endswith(" 1.0")
    await worker.close()
    body = await _until(app, "apipi_worker_disconnects_total")
    assert metric_line(body, "apipi_worker_disconnects_total", reason="clean").endswith(
        " 1.0"
    )
    assert "apipi_worker_info{" not in body
    assert metric_line(body, "apipi_worker_connections", run_mode="none").endswith(
        " 0.0"
    )
    lines = {
        field(record, "event"): record
        for record in caplog.records
        if getattr(record, "event", None)
        in {"worker.connected", "worker.hello.sent", "worker.disconnected"}
    }
    assert set(lines) == {
        "worker.connected",
        "worker.hello.sent",
        "worker.disconnected",
    }
    for record in lines.values():
        assert field(record, "connection_id") == connection_id
        assert field(record, "worker_id") == worker.worker_id
    assert field(lines["worker.disconnected"], "reason") == "clean"


async def test_register_reject_is_a_connect_result(
    settings: Settings, store: Store
) -> None:
    app = create_app(
        api_settings_for(settings.model_copy(update={"metrics": True})), store=store
    )
    worker = FakeWorker(app, "apw_not-a-real-token")
    await worker.ws.connect()
    await worker.receive_json()
    body = app.state.metrics.scrape().decode()
    assert metric_line(
        body, "apipi_worker_connects_total", result="unauthorized"
    ).endswith(" 1.0")
