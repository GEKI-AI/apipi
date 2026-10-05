import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for, split_client_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.search import SearchService
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    append_event,
    create_turn,
    set_session_lease,
    update_session,
)
from apipi.worker.fake_harness import FakeHarness

TAVILY_BODY = {
    "results": [{"title": "One", "url": "https://one.example/a", "content": "first"}],
    "usage": {"credits": 1},
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _code(response: httpx.Response) -> str | None:
    error = response.json().get("error")
    return error.get("code") if isinstance(error, dict) else None


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "store"),
        search_provider="tavily",
        search_api_key="secret-key",
    )


@pytest.fixture
async def unconfigured(
    settings: Settings, store: Store, worker_secret: str
) -> AsyncIterator[AsyncClient]:
    plain = settings.model_copy(
        update={"search_provider": None, "search_api_key": None}
    )
    async with split_client_for(
        plain, store, harness=FakeHarness(), token=worker_secret
    ) as (_app, client, _worker):
        yield client


async def _agent(client: AsyncClient, token: str, **body: Any) -> httpx.Response:
    return await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", **body},
    )


def _domains(domains: list[str]) -> dict[str, Any]:
    return {"type": "web_search", "filters": {"allowed_domains": domains}}


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        _domains(["example.com"]),
        _domains([f"d{i}.example.com" for i in range(10)]),
    ],
)
async def test_web_search_tool_round_trips(
    client: AsyncClient, tool: dict[str, Any]
) -> None:
    token = "ws-create"
    tools = [tool, {"type": "function", "name": "lookup", "parameters": {}}]
    created = await _agent(
        client,
        token,
        tools=tools,
        session_defaults={"environment": {"type": "none"}},
    )
    assert created.status_code == 200, created.text
    assert created.json()["tools"][0] == tool
    fetched = await client.get(
        f"/v1/agents/{created.json()['id']}", headers=_auth(token)
    )
    assert fetched.json()["tools"][0] == tool


@pytest.mark.parametrize(
    ("tool", "error_type", "code"),
    [
        ({"type": "web_search", "bogus": 1}, "invalid_request", "unknown_field"),
        (
            {"type": "web_search", "filters": {"bogus": 1}},
            "invalid_request",
            "unknown_field",
        ),
        (
            {"type": "web_search", "search_context_size": "high"},
            "not_implemented",
            "search_context_size",
        ),
        (
            {"type": "web_search", "user_location": {"type": "approximate"}},
            "not_implemented",
            "user_location",
        ),
        ({"type": "web_search_preview"}, "not_implemented", "web_search_preview"),
        (
            {"type": "web_search_preview_2025_03_11"},
            "not_implemented",
            "web_search_preview",
        ),
        (
            _domains([f"d{i}.example.com" for i in range(11)]),
            "invalid_request",
            "validation_error",
        ),
        (_domains(["https://example.com"]), "invalid_request", "validation_error"),
        (_domains(["example.com/path"]), "invalid_request", "validation_error"),
        (_domains([""]), "invalid_request", "validation_error"),
        (_domains(["a b.com"]), "invalid_request", "validation_error"),
    ],
)
async def test_invalid_web_search_tool(
    client: AsyncClient, tool: dict[str, Any], error_type: str, code: str
) -> None:
    response = await _agent(client, "ws-invalid", tools=[tool])
    assert response.status_code == 400
    assert response.json()["error"]["type"] == error_type
    assert _code(response) == code


async def test_agent_without_search_provider_is_400(unconfigured: AsyncClient) -> None:
    created = await _agent(unconfigured, "ws-none", tools=[{"type": "web_search"}])
    assert created.status_code == 400
    assert _code(created) == "search_not_configured"
    ok = await _agent(
        unconfigured, "ws-none", tools=[{"type": "function", "name": "x"}]
    )
    assert ok.status_code == 200, ok.text


async def test_agent_update_checks_search(
    client: AsyncClient, unconfigured: AsyncClient
) -> None:
    token = "ws-update"
    created = await _agent(client, token)
    assert created.status_code == 200
    updated = await client.post(
        f"/v1/agents/{created.json()['id']}",
        headers=_auth(token),
        json={"tools": [{"type": "web_search"}]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["tools"] == [{"type": "web_search"}]
    other = await _agent(unconfigured, "ws-update-none")
    assert other.status_code == 200
    denied = await unconfigured.post(
        f"/v1/agents/{other.json()['id']}",
        headers=_auth("ws-update-none"),
        json={"tools": [{"type": "web_search"}]},
    )
    assert denied.status_code == 400
    assert _code(denied) == "search_not_configured"
    renamed = await unconfigured.post(
        f"/v1/agents/{other.json()['id']}",
        headers=_auth("ws-update-none"),
        json={"name": "renamed"},
    )
    assert renamed.status_code == 200


async def test_inline_session_checks_search(
    client: AsyncClient, unconfigured: AsyncClient
) -> None:
    body = {
        "agent": {"model": "test", "tools": [{"type": "web_search"}]},
        "environment": {"type": "none"},
    }
    ok = await client.post("/v1/agents/sessions", headers=_auth("ws-inline"), json=body)
    assert ok.status_code == 200, ok.text
    denied = await unconfigured.post(
        "/v1/agents/sessions", headers=_auth("ws-inline-none"), json=body
    )
    assert denied.status_code == 400
    assert _code(denied) == "search_not_configured"


async def test_bundle_export_and_template_create_keep_tool(
    client: AsyncClient, unconfigured: AsyncClient
) -> None:
    token = "ws-bundle"
    tools = [{"type": "web_search", "filters": {"allowed_domains": ["example.com"]}}]
    created = await _agent(client, token, tools=tools)
    assert created.status_code == 200, created.text
    exported = await client.get(
        f"/v1/apipi/agents/{created.json()['id']}/export", headers=_auth(token)
    )
    assert exported.status_code == 200, exported.text
    imported = await client.post(
        "/v1/apipi/templates/import",
        headers=_auth(token),
        files={"bundle": ("a.apipi-agent.zip", exported.content, "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    made = await client.post(
        f"/v1/apipi/templates/{imported.json()['id']}/agents",
        headers=_auth(token),
        json={},
    )
    assert made.status_code == 200, made.text
    assert made.json()["agent"]["tools"] == tools
    denied_import = await unconfigured.post(
        "/v1/apipi/templates/import",
        headers=_auth("ws-bundle-none"),
        files={"bundle": ("a.apipi-agent.zip", exported.content, "application/zip")},
    )
    assert denied_import.status_code == 200, denied_import.text
    denied = await unconfigured.post(
        f"/v1/apipi/templates/{denied_import.json()['id']}/agents",
        headers=_auth("ws-bundle-none"),
        json={},
    )
    assert denied.status_code == 400
    assert _code(denied) == "search_not_configured"


async def _session_for(
    client: AsyncClient, token: str, tools: list[dict[str, Any]]
) -> uuid.UUID:
    agent = await _agent(client, token, tools=tools)
    assert agent.status_code == 200, agent.text
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert session.status_code == 200, session.text
    return uuid.UUID(session.json()["id"])


async def test_turn_context_sets_web_search(
    client: AsyncClient, settings: Settings, store: Store
) -> None:
    token = "ws-context"
    with_tool = await _session_for(client, token, [{"type": "web_search"}])
    without = await _session_for(client, token, [{"type": "function", "name": "x"}])
    tenant_id = _tenant(token)
    on = await build_turn_context(store, settings, tenant_id, with_tool)
    off = await build_turn_context(store, settings, tenant_id, without)
    assert on["agent"]["web_search"] is True
    assert off["agent"]["web_search"] is False
    dumped = json.dumps(on)
    assert "secret-key" not in dumped
    assert "tavily" not in dumped


async def test_turn_context_denies_when_resolver_returns_none(
    client: AsyncClient,
    settings: Settings,
    store: Store,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "ws-context-denied"
    session_id = await _session_for(client, token, [{"type": "web_search"}])
    plain = settings.model_copy(
        update={"search_provider": None, "search_api_key": None}
    )
    with caplog.at_level(logging.WARNING, logger="apipi.search"):
        context = await build_turn_context(store, plain, _tenant(token), session_id)
    assert context["agent"]["web_search"] is False
    records = [r for r in caplog.records if getattr(r, "event", "") == "search.denied"]
    assert len(records) == 1
    assert str(session_id) in str(records[0].__dict__)


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )


async def _socket_case(
    settings: Settings,
    store: Store,
    worker_secret: str,
    handler: Any,
    token: str,
) -> tuple[FakeWorker, uuid.UUID, uuid.UUID, FastAPI]:
    app = create_app(
        api_settings_for(
            _worker_settings(settings).model_copy(
                update={"search_provider": "tavily", "search_api_key": "secret-key"}
            )
        ),
        store=store,
    )
    tenant_id = _tenant(token)
    app.state.search = SearchService(
        store,
        app.state.settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        agent = await _agent(http, token, tools=[{"type": "web_search"}])
        assert agent.status_code == 200, agent.text
        session = await http.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        assert session.status_code == 200, session.text
    session_id = uuid.UUID(session.json()["id"])
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    worker_id = uuid.UUID(str(hello["worker_id"]))
    turn_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    async with store.session() as db:
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
    conn = app.state.workers.get(worker_id)
    assert conn is not None
    conn.leases.add(lease_id)
    async with store.session() as db:
        await update_session(
            db, tenant_id, session_id, changes={"status": "in_progress"}
        )
        await create_turn(
            db, tenant_id, session_id, status="in_progress", turn_id=turn_id
        )
        await append_event(
            db,
            tenant_id,
            session_id,
            type="agent.session.turn.created",
            data={"turn_id": str(turn_id)},
        )
    return worker, session_id, turn_id, app


def _request(
    session_id: uuid.UUID, turn_id: uuid.UUID, **values: Any
) -> dict[str, Any]:
    return {
        "type": "search.request",
        "request_id": str(uuid.uuid4()),
        "session_id": str(session_id),
        "turn_id": str(turn_id),
        "query": "pi agents",
        "max_results": None,
        **values,
    }


async def _reply(worker: FakeWorker, request_id: str) -> dict[str, Any]:
    for _ in range(20):
        message = await worker.receive_json(timeout=10)
        if message.get("type") == "search.reply":
            assert message["request_id"] == request_id
            return message
    raise AssertionError("no search.reply")


async def test_socket_concurrent_requests_do_not_block_each_other(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if "slow" in json.loads(request.content)["query"]:
            await gate.wait()
        return httpx.Response(200, json=TAVILY_BODY)

    worker, session_id, turn_id, _app = await _socket_case(
        settings, store, worker_secret, handler, "ws-concurrent"
    )
    try:
        slow = _request(session_id, turn_id, query="slow one")
        fast = _request(session_id, turn_id, query="fast one")
        await worker.send_json(slow)
        await worker.send_json(fast)
        first = await _reply(worker, fast["request_id"])
        assert first["ok"] is True
        gate.set()
        second = await _reply(worker, slow["request_id"])
        assert second["ok"] is True
    finally:
        gate.set()
        await worker.close()


async def test_socket_survives_garbage_and_service_errors(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    worker, session_id, turn_id, app = await _socket_case(
        settings,
        store,
        worker_secret,
        lambda request: httpx.Response(200, json=TAVILY_BODY),
        "ws-socket-garbage",
    )

    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    try:
        await worker.send_json({"type": "search.request", "request_id": "nope"})
        await worker.send_json({"type": "search.request", "session_id": "x"})
        app.state.search.handle_request = broken
        request = _request(session_id, turn_id)
        await worker.send_json(request)
        reply = await _reply(worker, request["request_id"])
        assert reply["ok"] is False
        assert reply["code"] == "search_failed"
        assert "boom" not in json.dumps(reply)
    finally:
        await worker.close()
