from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.otel import Tracing
from apipi.services.runtime import FAKE_USAGE, FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _blob(spans: object) -> str:
    parts: list[str] = []
    if not isinstance(spans, list):
        return str(spans)
    for span in spans:
        parts.append(getattr(span, "name", ""))
        parts.append(str(dict(getattr(span, "attributes", None) or {})))
        events = getattr(span, "events", None)
        if events:
            parts.append(str(events))
    return "".join(parts)


@pytest.fixture
def otel_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def otel_tracing(otel_exporter: InMemorySpanExporter) -> Iterator[Tracing]:
    tracing = Tracing(exporter=otel_exporter)
    yield tracing
    tracing.shutdown()


@pytest.fixture
async def otel_client(
    settings: Settings, store: Store, otel_tracing: Tracing
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=FakeHarness(), tracing=otel_tracing)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def tool_harness() -> FakeHarness:
    harness = FakeHarness()
    harness.function_calls = [
        {"name": "echo", "arguments": {"text": "hi"}, "call_id": "call_1"}
    ]
    return harness


@pytest.fixture
async def otel_tool_client(
    settings: Settings, store: Store, otel_tracing: Tracing, tool_harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=tool_harness, tracing=otel_tracing)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def test_create_app_tracing_off(settings: Settings, store: Store) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    assert app.state.tracing is None


def test_create_app_otlp_when_endpoint_set(tmp_path: Path, store: Store) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        otel_endpoint="http://otel:4318",
    )
    app = create_app(settings, store=store, harness=FakeHarness())
    try:
        assert isinstance(app.state.tracing, Tracing)
    finally:
        app.state.tracing.shutdown()


async def test_traceparent_parents_session_span(
    otel_client: AsyncClient, otel_exporter: InMemorySpanExporter
) -> None:
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    token = "otel-parent"
    agent = await otel_client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await otel_client.post(
        "/v1/agents/sessions",
        headers={
            **_auth(token),
            "traceparent": f"00-{trace_id}-b7ad6b7169203331-01",
        },
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    assert created.headers["x-trace-id"] == trace_id
    spans = list(otel_exporter.get_finished_spans())
    session = next(span for span in spans if span.name == "session")
    assert session.context.trace_id == int(trace_id, 16)
    assert session.parent is not None
    assert session.parent.span_id == int("b7ad6b7169203331", 16)


async def test_completed_turn_spans_link_ids_without_message_text(
    otel_client: AsyncClient, otel_exporter: InMemorySpanExporter
) -> None:
    token = "otel-t"
    agent = await otel_client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await otel_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    turns = await otel_client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    turn_id = turns.json()["data"][0]["id"]
    request_id = created.headers["x-request-id"]
    spans = list(otel_exporter.get_finished_spans())
    by_name = {span.name: span for span in spans}
    assert set(by_name) >= {"session", "turn", "model"}
    session = by_name["session"]
    turn = by_name["turn"]
    model = by_name["model"]
    assert turn.parent is not None
    assert turn.parent.span_id == session.context.span_id
    assert model.parent is not None
    assert model.parent.span_id == turn.context.span_id
    session_attrs = dict(session.attributes or {})
    turn_attrs = dict(turn.attributes or {})
    model_attrs = dict(model.attributes or {})
    assert session_attrs["request_id"] == request_id
    assert session_attrs["session_id"] == session_id
    assert session_attrs["model"] == "test"
    assert turn_attrs["request_id"] == request_id
    assert turn_attrs["session_id"] == session_id
    assert turn_attrs["turn_id"] == turn_id
    assert turn_attrs["model"] == "test"
    assert turn_attrs["status"] == "completed"
    assert turn_attrs["prompt_tokens"] == FAKE_USAGE["prompt_tokens"]
    assert turn_attrs["completion_tokens"] == FAKE_USAGE["completion_tokens"]
    assert turn_attrs["cache_read_tokens"] == FAKE_USAGE["cache_read_tokens"]
    assert turn_attrs["cache_write_tokens"] == FAKE_USAGE["cache_write_tokens"]
    assert turn_attrs["total_tokens"] == FAKE_USAGE["total_tokens"]
    assert model_attrs["request_id"] == request_id
    assert model_attrs["session_id"] == session_id
    assert model_attrs["turn_id"] == turn_id
    assert model_attrs["model"] == "test"
    assert model_attrs["prompt_tokens"] == FAKE_USAGE["prompt_tokens"]
    blob = _blob(spans)
    assert "hello" not in blob
    assert "secret-prompt" not in blob


async def test_function_tool_turn_span_has_tool_names(
    otel_tool_client: AsyncClient, otel_exporter: InMemorySpanExporter
) -> None:
    token = "otel-tools"
    agent = await otel_tool_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "bot",
            "model": "test",
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )
    created = await otel_tool_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "use echo",
        },
    )
    session_id = created.json()["id"]
    events = await otel_tool_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    require = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.requires_action"
    ]
    turn_id = require[0]["data"]["turn_id"]
    otel_exporter.clear()
    resumed = await otel_tool_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={
            "type": "agent.session.input.tool_result",
            "turn_id": turn_id,
            "call_id": "call_1",
            "success": True,
            "output": "pong",
        },
    )
    assert resumed.status_code == 200
    spans = list(otel_exporter.get_finished_spans())
    turn = next(span for span in spans if span.name == "turn")
    attrs = dict(turn.attributes or {})
    assert attrs["turn_id"] == turn_id
    assert attrs["session_id"] == session_id
    assert attrs["status"] == "completed"
    assert attrs["tool_names"] == ("echo",)
    blob = _blob(spans)
    assert "use echo" not in blob
    assert "pong" not in blob
    assert "hello" not in blob
