import json
import logging

import httpx
import pytest
from tests.support import fake_sink

from apipi.config import ConfigError, Settings
from apipi.services.usage_export import UsageExporter, export_usage, load_usage_sinks

_OriginalClient = httpx.AsyncClient


def _settings(*, retries: int = 1) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        usage_export_url="http://export.test/usage",
        usage_export_retries=retries,
    )


class _Client:
    def __init__(self, transport: httpx.MockTransport) -> None:
        self._client = _OriginalClient(transport=transport)

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()


async def test_usage_export_posts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apipi.services.usage_export.httpx.AsyncClient",
        lambda **_kwargs: _Client(transport),
    )
    await UsageExporter(_settings())._post({"tenant_id": "t", "status": "completed"})
    assert len(captured) == 1
    assert json.loads(captured[0].content) == {
        "tenant_id": "t",
        "status": "completed",
    }


async def test_usage_export_drop_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apipi.services.usage_export.httpx.AsyncClient",
        lambda **_kwargs: _Client(transport),
    )
    await UsageExporter(_settings(retries=0))._post(
        {
            "tenant_id": "t1",
            "session_id": "s1",
            "turn_id": "x",
            "request_id": "r1",
        }
    )
    dropped = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "usage.export.dropped"
    ]
    assert dropped
    last = dropped[-1]
    assert last.__dict__["error_code"] == "export_drop"
    assert last.__dict__["tenant_id"] == "t1"
    assert last.__dict__["session_id"] == "s1"
    assert last.__dict__["turn_id"] == "x"
    assert last.__dict__["request_id"] == "r1"


def test_custom_usage_sink_receives_event() -> None:
    fake_sink.reset()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        usage_sinks="tests.support.fake_sink:FakeSink",
    )
    event = {"tenant_id": "t", "status": "completed"}
    export_usage(settings, None, event)
    assert fake_sink.events == [event]


def test_missing_usage_sink_fails_at_load() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        usage_sinks="tests.support.missing_sink:Nope",
    )
    with pytest.raises(ConfigError, match="sink not found"):
        load_usage_sinks(settings)


async def test_usage_export_includes_search_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from datetime import UTC, datetime

    from apipi.services.usage import usage_event

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "apipi.services.usage_export.httpx.AsyncClient",
        lambda **_kwargs: _Client(transport),
    )
    event = usage_event(
        tenant_id=uuid.uuid4(),
        key_id="k",
        session_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        agent_id=None,
        model="m",
        status="completed",
        latency_ms=1,
        usage={},
        tool_names=[],
        tool_counts={},
        mcp_names=[],
        mcp_counts={},
        environment_type="none",
        run_mode="none",
        instance_id=None,
        artifact_bytes=0,
        request_id=None,
        error_code=None,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
        user_id="u",
        search_calls=3,
        search_units=4,
        search_counts={"tavily/operator": {"calls": 3, "units": 4}},
    )
    await UsageExporter(_settings())._post(event)
    body = json.loads(captured[0].content)
    assert body["search_calls"] == 3
    assert body["search_units"] == 4
    assert body["search_counts"] == {"tavily/operator": {"calls": 3, "units": 4}}
    assert body["user_id"] == "u"
