"""Split-mode end to end: streaming deltas and HTTP MCP over a remote worker.

Acceptance test for #441. One API-only app serves HTTP while a
credential-less worker (no ``DATABASE_URL``, no object-store
credentials, ``store=None``) connects over ``/internal/worker`` through
the real worker protocol and runs turns from the command context. The
worker side is driven by the real worker connection loop
(``_serve_connection``) over a small socket adapter around
``FakeWorker``. A streaming fake model host emits several text chunks
per turn and really calls the fake HTTP MCP server from the turn
context (URL plus vault headers), building the ``mcp_call`` item from
the live JSON-RPC result.

The test asserts that the SSE client sees multiple
``agent.session.turn.output_text.delta`` events before
``output_text.done`` with the concatenated deltas equal to the final
text, that the MCP server saw the vault bearer on both turns and both
``mcp_call`` items are stored, and that deltas are never stored
(``after_seq`` replay and export are unchanged). With a Postgres test
database a second API replica streams the same deltas over the shared
event bus.
"""

import asyncio
import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker

from apipi.api.sessions import _event_stream
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.event_bus import InMemoryEventBus, PostgresEventBus
from apipi.services.runtime import FakeHarness, usage_from
from apipi.store.engine import Store
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
from apipi.worker.execution import RemoteExecution, local_execution
from apipi.worker.hub import CommandDedupe, _serve_connection
from apipi.worker.outbox import Outbox

pytest_plugins = ["tests.support.mcp_http_server"]

CHUNKS_FIRST = ["hel", "lo ", "wor", "ld"]
TEXT_FIRST = "".join(CHUNKS_FIRST)
CHUNKS_SECOND = ["sec", "ond ", "turn"]
TEXT_SECOND = "".join(CHUNKS_SECOND)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid5(NAMESPACE_URL, hash_token(token))


def _api_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        local_store_dir=settings.sessions_dir,
        api_only=True,
        mcp_allow_hosts="127.0.0.1",
    )


def _parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        data = None
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = line[6:]
        if data is not None:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            events.append(parsed)
    return events


def _server_entry(entry: Any) -> tuple[str, str, dict[str, str]] | None:
    """Read an ``mcp_http`` turn-context entry as label, URL, headers."""
    if isinstance(entry, dict):
        label, url, headers = (
            entry.get("server_label"),
            entry.get("server_url"),
            entry.get("headers"),
        )
    else:
        label, url, headers = (
            getattr(entry, "server_label", None),
            getattr(entry, "server_url", None),
            getattr(entry, "headers", None),
        )
    if isinstance(label, str) and isinstance(url, str) and isinstance(headers, dict):
        return label, url, {str(key): str(value) for key, value in headers.items()}
    return None


class StreamingMcpHarness(FakeHarness):
    """Fake model host: spaced text chunks plus live MCP tool calls.

    Scripted calls name a tool on an ``mcp_http`` server from the turn
    context. The harness performs a JSON-RPC ``tools/call`` against
    that server with the entry's URL and headers (so the vault bearer)
    and builds the ``mcp_call`` item from the real result.
    """

    def __init__(self, chunks: list[str]) -> None:
        super().__init__()
        self.chunks = list(chunks)
        self.mcp_http_turns: list[Any] = []
        self.mcp_http_calls: list[dict[str, str]] = []

    async def _call_tool(
        self, server: tuple[str, str, dict[str, str]], name: str
    ) -> Any:
        label, url, headers = server
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                },
            )
        assert response.status_code == 200, f"MCP {label} call failed"
        body = response.json()
        assert isinstance(body, dict) and body.get("result") is not None
        self.mcp_http_calls.append(
            {
                "server_label": label,
                "url": url,
                "authorization": headers.get("Authorization", ""),
            }
        )
        return body["result"]

    async def generate(self, text: str, **kwargs: object):  # type: ignore[override]
        raw_mcp = kwargs.get("mcp_http")
        entries = raw_mcp if isinstance(raw_mcp, list) else []
        self.mcp_http = list(entries)
        self.mcp_http_turns.append(self.mcp_http)
        for call in self.mcp_calls:
            assert isinstance(call, dict)
            server = None
            for entry in entries:
                parsed = _server_entry(entry)
                if parsed is not None and parsed[0] == call.get("server_label"):
                    server = parsed
                    break
            assert server is not None, (
                f"no mcp_http server for {call.get('server_label')}"
            )
            result = await self._call_tool(server, str(call.get("name")))
            yield (
                "agent.session.turn.item.added",
                {
                    "item_type": "mcp_call",
                    "call_id": call.get("call_id"),
                    "name": call.get("name"),
                    "server_label": server[0],
                    "result": result,
                },
            )
        self.mcp_calls = []
        for chunk in self.chunks:
            yield ("agent.session.turn.output_text.delta", {"delta": chunk})
            await asyncio.sleep(0.05)
        yield ("agent.session.turn.output_text.done", {"text": "".join(self.chunks)})
        yield ("usage", usage_from(self.usage))


def _block_worker_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("split worker must not construct storage clients")

    import apipi.services.runtime as runtime
    import apipi.store.blobs as blobs
    import apipi.store.engine as engine

    monkeypatch.setattr(engine, "create_engine", _boom)
    monkeypatch.setattr(engine, "Store", _boom)
    monkeypatch.setattr(runtime, "object_store", _boom)
    monkeypatch.setattr(blobs, "object_store", _boom)
    monkeypatch.setattr(blobs, "blob_store", _boom)
    monkeypatch.setattr(blobs, "S3Store", _boom)


async def _collect_until_completed(stream: Any) -> str:
    chunks: list[str] = []
    try:
        async with asyncio.timeout(60):
            async for chunk in stream:
                if chunk.startswith(":"):
                    continue
                chunks.append(chunk)
                types = [event["type"] for event in _parse_sse("".join(chunks))]
                if "agent.session.turn.completed" in types:
                    return "".join(chunks)
    finally:
        await stream.aclose()
    raise AssertionError("turn never completed on SSE")


def _assert_streamed_text(raw: str, expected: str, minimum_deltas: int = 3) -> None:
    events = _parse_sse(raw)
    kinds = [event["type"] for event in events]
    assert "agent.session.turn.output_text.done" in kinds
    deltas = [
        event
        for event in events
        if event["type"] == "agent.session.turn.output_text.delta"
    ]
    assert len(deltas) >= minimum_deltas
    first_done = kinds.index("agent.session.turn.output_text.done")
    delta_positions = [
        i
        for i, kind in enumerate(kinds)
        if kind == "agent.session.turn.output_text.delta"
    ]
    assert delta_positions and max(delta_positions) < first_done
    done = next(
        event
        for event in events
        if event["type"] == "agent.session.turn.output_text.done"
    )
    assert done["data"]["text"] == expected
    assert "".join(str(delta["data"]["delta"]) for delta in deltas) == expected


class _WorkerSocket:
    """Adapt ``FakeWorker`` to the socket shape ``_serve_connection`` needs.

    The real worker connection loop only uses ``send(str)`` and
    ``recv()``; this adapter forwards both over the in-process ASGI
    websocket and records ``delta.text`` payloads crossing the wire.
    """

    def __init__(self, worker: FakeWorker) -> None:
        self._worker = worker
        self.delta_texts: list[str] = []
        self.hello_seen = asyncio.Event()

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        assert isinstance(payload, dict)
        if payload.get("type") == "delta.text":
            data = payload.get("payload")
            if isinstance(data, dict):
                self.delta_texts.append(str(data.get("text", "")))
        await self._worker.send_json(payload)

    async def recv(self) -> str:
        message = await self._worker.receive_json(timeout=30)
        if message.get("ok") and "worker_id" in message:
            self.hello_seen.set()
        return json.dumps(message)


async def test_split_mode_deltas_and_http_mcp(
    settings: Settings,
    store: Store,
    worker_secret: str,
    mcp_server: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_url, seen = mcp_server
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert os.environ.get("DATABASE_URL") is None
    api_settings = _api_settings(settings)
    app = create_app(api_settings, store=store, harness=FakeHarness())
    assert isinstance(app.state.execution, RemoteExecution)

    worker_settings = Settings(
        run_mode="none",
        sessions_dir=str(tmp_path / "worker-sessions"),
        # The shared local store root must be mounted at the same path as
        # the API (production split-mode mounts one volume there); the
        # worker answers the hello store challenge from it. The worker
        # sessions dir stays separate: workspaces are per-worker.
        local_store_dir=settings.sessions_dir,
        mcp_allow_hosts="127.0.0.1",
    )
    bus = InMemoryEventBus()
    relay = DeltaRelay()
    outbox = Outbox()
    harness = StreamingMcpHarness(CHUNKS_FIRST)
    harness.mcp_calls = [
        {"call_id": "call-1", "name": "mock_tool", "server_label": "mock"}
    ]
    execution = local_execution(
        worker_settings,
        store=None,
        harness=harness,
        hub=LiveRedirectBus(bus, relay),
        outbox=outbox,
    )
    assert execution.store is None
    assert execution.blobs is None
    assert execution.objects is None
    _block_worker_storage(monkeypatch)

    worker = FakeWorker(app, worker_secret)
    await worker.ws.connect()
    sock = _WorkerSocket(worker)
    worker_tasks: set[asyncio.Task[None]] = set()
    serve_task = asyncio.create_task(
        _serve_connection(
            worker_settings,
            execution,
            outbox,
            relay,
            sock,
            session_leases={},
            command_tasks=set(),
            tasks=worker_tasks,
            draining=asyncio.Event(),
            drain_deadline=None,
            wait=60.0,
            heartbeat=1.0,
            emitter=None,
            dedupe=CommandDedupe(),
        )
    )
    try:
        async with asyncio.timeout(30):
            await sock.hello_seen.wait()
    except TimeoutError:
        serve_task.cancel()
        failure = serve_task.exception() if serve_task.done() else None
        raise AssertionError(
            f"worker never finished register/hello: {failure!r}"
            if failure is not None
            else "worker never finished register/hello"
        ) from None

    token = "split-441"
    tenant_id = _tenant(token)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            vault = await client.post(
                "/v1/agents/vaults", headers=_auth(token), json={"name": "v"}
            )
            assert vault.status_code == 200
            cred = await client.post(
                f"/v1/agents/vaults/{vault.json()['id']}/credentials",
                headers=_auth(token),
                json={
                    "name": "c",
                    "auth": {
                        "type": "static_bearer",
                        "mcp_server_url": mcp_url,
                        "token": "vault-secret",
                    },
                },
            )
            assert cred.status_code == 200
            agent = await client.post(
                "/v1/agents",
                headers=_auth(token),
                json={
                    "name": "bot",
                    "model": "test",
                    "tools": [
                        {
                            "type": "mcp",
                            "server_label": "mock",
                            "server_url": mcp_url,
                        }
                    ],
                },
            )
            assert agent.status_code == 200
            created = await client.post(
                "/v1/agents/sessions",
                headers=_auth(token),
                json={
                    "agent_id": agent.json()["id"],
                    "environment": {"type": "none"},
                    "vault_ids": [vault.json()["id"]],
                },
            )
            assert created.status_code == 200
            session_id = uuid.UUID(created.json()["id"])

            second_raw: str | None = None
            if os.environ.get("APIPI_TEST_DATABASE_URL"):
                assert isinstance(app.state.event_hub, PostgresEventBus), (
                    "second-replica block needs the shared Postgres bus"
                )
                second_app = create_app(
                    api_settings, store=store, harness=FakeHarness()
                )
                assert isinstance(second_app.state.event_hub, PostgresEventBus)
                await second_app.state.event_hub.start()
                try:
                    second_stream = _event_stream(
                        store,
                        second_app.state.event_hub,
                        tenant_id,
                        session_id,
                        None,
                        fallback_poll=0.05,
                    )
                    second_collect = asyncio.create_task(
                        _collect_until_completed(second_stream)
                    )
                    first_stream = _event_stream(
                        store,
                        app.state.event_hub,
                        tenant_id,
                        session_id,
                        None,
                        fallback_poll=0.05,
                    )
                    first_collect = asyncio.create_task(
                        _collect_until_completed(first_stream)
                    )
                    posted = await client.post(
                        f"/v1/agents/sessions/{session_id}/events",
                        headers=_auth(token),
                        json={
                            "type": "agent.session.input.message",
                            "content": "first",
                        },
                    )
                    assert posted.status_code == 200
                    first_raw = await first_collect
                    second_raw = await second_collect
                finally:
                    await second_app.state.event_hub.close()
            else:
                stream = _event_stream(
                    store,
                    app.state.event_hub,
                    tenant_id,
                    session_id,
                    None,
                    fallback_poll=0.05,
                )
                collect = asyncio.create_task(_collect_until_completed(stream))
                posted = await client.post(
                    f"/v1/agents/sessions/{session_id}/events",
                    headers=_auth(token),
                    json={"type": "agent.session.input.message", "content": "first"},
                )
                assert posted.status_code == 200
                first_raw = await collect

            _assert_streamed_text(first_raw, TEXT_FIRST)
            if second_raw is not None:
                _assert_streamed_text(second_raw, TEXT_FIRST)
            assert sock.delta_texts, "worker sent no delta.text envelopes"
            assert "".join(sock.delta_texts).startswith(TEXT_FIRST)
            assert harness.mcp_http_turns, "worker harness saw no turn"
            first_mcp = harness.mcp_http_turns[0]
            assert first_mcp is not None and first_mcp[0].server_label == "mock"
            assert first_mcp[0].headers == {"Authorization": "Bearer vault-secret"}

            harness.chunks = list(CHUNKS_SECOND)
            harness.mcp_calls = [
                {"call_id": "call-2", "name": "mock_tool", "server_label": "mock"}
            ]
            before = len(sock.delta_texts)
            mid_stored = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            assert mid_stored.status_code == 200
            mid_events = mid_stored.json()["data"]
            assert mid_events, "first turn stored no events"
            follow_stream = _event_stream(
                store,
                app.state.event_hub,
                tenant_id,
                session_id,
                mid_events[-1]["seq"],
                fallback_poll=0.05,
            )
            follow_collect = asyncio.create_task(
                _collect_until_completed(follow_stream)
            )
            follow_posted = await client.post(
                f"/v1/agents/sessions/{session_id}/events",
                headers=_auth(token),
                json={"type": "agent.session.input.message", "content": "second"},
            )
            assert follow_posted.status_code == 200
            follow_raw = await follow_collect
            _assert_streamed_text(follow_raw, TEXT_SECOND)
            assert len(sock.delta_texts) > before
            assert "".join(sock.delta_texts) == (TEXT_FIRST + TEXT_SECOND)
            assert len(harness.mcp_http_turns) >= 2
            second_mcp = harness.mcp_http_turns[-1]
            assert second_mcp is not None and second_mcp[0].server_label == "mock"
            assert second_mcp[0].headers == {"Authorization": "Bearer vault-secret"}
            assert seen.get("Authorization") == "Bearer vault-secret"
            assert [call["authorization"] for call in harness.mcp_http_calls] == [
                "Bearer vault-secret",
                "Bearer vault-secret",
            ]

            stored = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            assert stored.status_code == 200
            stored_events = stored.json()["data"]
            stored_types = [event["type"] for event in stored_events]
            assert "agent.session.turn.output_text.delta" not in stored_types
            mcp_items = [
                event
                for event in stored_events
                if event["type"] == "agent.session.turn.item.added"
                and event["data"].get("item_type") == "mcp_call"
            ]
            assert len(mcp_items) == 2
            assert [item["data"]["call_id"] for item in mcp_items] == [
                "call-1",
                "call-2",
            ]
            assert {item["data"]["server_label"] for item in mcp_items} == {"mock"}
            assert {item["data"]["name"] for item in mcp_items} == {"mock_tool"}
            for item in mcp_items:
                result = item["data"].get("result")
                assert isinstance(result, dict)
                assert result.get("serverInfo", {}).get("name") == "mock"

            last_seq = stored_events[-1]["seq"]
            replay_stream = _event_stream(
                store, app.state.event_hub, tenant_id, session_id, last_seq
            )
            replayed: list[dict[str, Any]] = []
            try:
                async with asyncio.timeout(2):
                    async for chunk in replay_stream:
                        if chunk.startswith(":"):
                            continue
                        replayed.extend(_parse_sse(chunk))
            except TimeoutError:
                pass
            finally:
                await replay_stream.aclose()
            assert replayed == []
            full_replay_stream = _event_stream(
                store, app.state.event_hub, tenant_id, session_id, 0
            )
            full_replayed: list[dict[str, Any]] = []
            try:
                async with asyncio.timeout(10):
                    async for chunk in full_replay_stream:
                        if chunk.startswith(":"):
                            continue
                        full_replayed.extend(_parse_sse(chunk))
                        if len(full_replayed) >= len(stored_events):
                            break
            finally:
                await full_replay_stream.aclose()
            assert [event["type"] for event in full_replayed] == stored_types

            exported = await client.get(
                f"/v1/apipi/sessions/{session_id}/export", headers=_auth(token)
            )
            assert exported.status_code == 200
            export_types = [event["type"] for event in exported.json()["events"]]
            assert "agent.session.turn.output_text.delta" not in export_types
            assert exported.json()["events"] == stored_events
    finally:
        serve_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await serve_task
        for task in list(worker_tasks):
            if not task.done():
                task.cancel()
        relay.detach()
        await worker.close()
        await execution.close()
        await bus.close()
