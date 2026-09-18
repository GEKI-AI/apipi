import asyncio
import uuid
from uuid import NAMESPACE_URL, uuid5

from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.support.fake_worker import FakeWorker
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.metrics import Metrics
from apipi.gateway.otel import Tracing
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FAKE_USAGE, FakeHarness
from apipi.store.engine import Store
from apipi.worker.execution import (
    RemoteExecution,
    local_execution,
    worker_observability,
)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _api_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
        api_only=True,
    )


async def test_api_only_uses_remote_execution(settings: Settings, store: Store) -> None:
    app = create_app(_api_settings(settings), store=store, harness=FakeHarness())
    assert isinstance(app.state.execution, RemoteExecution)


async def test_api_only_without_worker_is_429(settings: Settings, store: Store) -> None:
    app = create_app(_api_settings(settings), store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth("t"), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("t"),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 429
        assert created.json()["error"]["code"] == "capacity"


async def test_remote_turn_via_worker(settings: Settings, store: Store) -> None:
    api_settings = _api_settings(settings)
    app = create_app(api_settings, store=store, harness=FakeHarness())
    local = local_execution(
        api_settings,
        store=store,
        harness=FakeHarness(),
        hub=app.state.event_hub,
        env_hub=app.state.env_hub,
    )
    token = "t"
    worker = FakeWorker(app, "worker-secret")
    ready = asyncio.Event()

    async def pump() -> None:
        hello = await worker.connect(capacity=2)
        assert hello.get("ok") is True
        ready.set()
        while True:
            message = await worker.receive_json()
            if message.get("type") != "command":
                continue
            await worker.send_json(
                {
                    "type": "lease.ack",
                    "id": message.get("id"),
                    "lease_id": message.get("lease_id"),
                }
            )
            from apipi.worker.hub import dispatch_command

            await dispatch_command(local, message)

    task = asyncio.create_task(pump())
    await ready.wait()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            agent = await client.post(
                "/v1/agents",
                headers=_auth(token),
                json={"name": "bot", "model": "test"},
            )
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={
                    "agent_id": agent.json()["id"],
                    "environment": {"type": "none"},
                    "input": "hello",
                },
            )
            assert created.status_code == 200
            body = created.json()
            assert body["status"] == "idle"
            session_id = uuid.UUID(body["id"])
            events = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            types = [event["type"] for event in events.json()["data"]]
            assert "agent.session.turn.completed" in types
    finally:
        task.cancel()
        await worker.close()
        await local.close()


async def test_remote_turn_records_metrics_and_spans_on_worker(
    settings: Settings, store: Store
) -> None:
    api_settings = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
        api_only=True,
        metrics=True,
    )
    app = create_app(api_settings, store=store, harness=FakeHarness())
    worker_metrics = Metrics()
    exporter = InMemorySpanExporter()
    tracing = Tracing(exporter=exporter)
    local = local_execution(
        api_settings,
        store=store,
        harness=FakeHarness(),
        hub=app.state.event_hub,
        env_hub=app.state.env_hub,
        metrics=worker_metrics,
        tracing=tracing,
    )
    token = "t"
    tenant = str(uuid5(NAMESPACE_URL, hash_token(token)))
    worker = FakeWorker(app, "worker-secret")
    ready = asyncio.Event()

    async def pump() -> None:
        hello = await worker.connect(capacity=2)
        assert hello.get("ok") is True
        ready.set()
        while True:
            message = await worker.receive_json()
            if message.get("type") != "command":
                continue
            await worker.send_json(
                {
                    "type": "lease.ack",
                    "id": message.get("id"),
                    "lease_id": message.get("lease_id"),
                }
            )
            from apipi.worker.hub import dispatch_command

            await dispatch_command(local, message)

    task = asyncio.create_task(pump())
    await ready.wait()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            agent = await client.post(
                "/v1/agents",
                headers=_auth(token),
                json={"name": "bot", "model": "test"},
            )
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={
                    "agent_id": agent.json()["id"],
                    "environment": {"type": "none"},
                    "input": "hello",
                },
            )
            assert created.status_code == 200
            api_metrics = await client.get("/metrics")
            assert api_metrics.status_code == 200
            api_body = api_metrics.text
            assert "apipi_turns_total{" not in api_body
            worker_body = worker_metrics.scrape().decode()
            assert metric_line(
                worker_body, "apipi_turns_total", tenant=tenant, status="completed"
            ).endswith(" 1.0")
            assert metric_line(
                worker_body, "apipi_tokens_total", tenant=tenant, kind="total"
            ).endswith(f" {float(FAKE_USAGE['total_tokens'])}")
            names = {span.name for span in exporter.get_finished_spans()}
            assert names >= {"turn", "model"}
    finally:
        task.cancel()
        await worker.close()
        await local.close()
        tracing.shutdown()


def test_worker_observability_follows_settings(settings: Settings) -> None:
    off_metrics, off_tracing = worker_observability(settings)
    assert off_metrics is None
    assert off_tracing is None
    on = settings.model_copy(
        update={"metrics": True, "otel_endpoint": "http://otel:4318"}
    )
    metrics, tracing = worker_observability(on)
    try:
        assert isinstance(metrics, Metrics)
        assert isinstance(tracing, Tracing)
    finally:
        if tracing is not None:
            tracing.shutdown()
