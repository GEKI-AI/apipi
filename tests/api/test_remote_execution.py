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


async def test_get_during_remote_turn_stays_in_progress(
    settings: Settings, store: Store
) -> None:
    api_settings = _api_settings(settings)
    app = create_app(api_settings, store=store, harness=FakeHarness())
    gate = asyncio.Event()

    class HoldHarness(FakeHarness):
        async def generate(self, text: str, **kwargs: object):  # type: ignore[override]
            del text, kwargs
            await gate.wait()
            yield ("agent.session.turn.output_text.done", {"text": "ok"})

    held = HoldHarness()
    local = local_execution(
        api_settings,
        store=store,
        harness=held,
        hub=app.state.event_hub,
        env_hub=app.state.env_hub,
    )
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
                headers=_auth("remote-get"),
                json={"name": "bot", "model": "test"},
            )
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth("remote-get"),
                json={
                    "agent_id": agent.json()["id"],
                    "environment": {"type": "none"},
                },
            )
            assert created.status_code == 200
            session_id = created.json()["id"]
            turn = asyncio.create_task(
                client.post(
                    f"/v1/agents/sessions/{session_id}/events",
                    headers=_auth("remote-get"),
                    json={
                        "type": "agent.session.input.message",
                        "content": "hello",
                    },
                )
            )
            status = "idle"
            for _ in range(40):
                got = await client.get(
                    f"/v1/agents/sessions/{session_id}",
                    headers=_auth("remote-get"),
                )
                status = got.json()["status"]
                if status == "in_progress":
                    break
                await asyncio.sleep(0.05)
            assert status == "in_progress"
            again = await client.get(
                f"/v1/agents/sessions/{session_id}",
                headers=_auth("remote-get"),
            )
            assert again.json()["status"] == "in_progress"
            gate.set()
            finished = await turn
            assert finished.status_code == 200
    finally:
        gate.set()
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


async def test_remote_turn_shares_trace_and_assign_span(
    settings: Settings, store: Store
) -> None:
    api_exporter = InMemorySpanExporter()
    api_tracing = Tracing(exporter=api_exporter)
    worker_exporter = InMemorySpanExporter()
    worker_tracing = Tracing(exporter=worker_exporter)
    api_settings = _api_settings(settings)
    app = create_app(
        api_settings, store=store, harness=FakeHarness(), tracing=api_tracing
    )
    local = local_execution(
        api_settings,
        store=store,
        harness=FakeHarness(),
        hub=app.state.event_hub,
        env_hub=app.state.env_hub,
        tracing=worker_tracing,
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
            api_spans = list(api_exporter.get_finished_spans())
            worker_spans = list(worker_exporter.get_finished_spans())
            api_names = {span.name for span in api_spans}
            worker_names = {span.name for span in worker_spans}
            assert api_names >= {"session", "worker.assign"}
            assert worker_names >= {"turn", "model"}
            session = next(span for span in api_spans if span.name == "session")
            assign = next(span for span in api_spans if span.name == "worker.assign")
            turn = next(span for span in worker_spans if span.name == "turn")
            assert assign.parent is not None
            assert assign.parent.span_id == session.context.span_id
            assert turn.context.trace_id == session.context.trace_id
    finally:
        task.cancel()
        await worker.close()
        await local.close()
        api_tracing.shutdown()
        worker_tracing.shutdown()


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
