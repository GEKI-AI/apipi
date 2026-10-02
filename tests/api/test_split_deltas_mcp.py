"""Split-mode end to end: streaming deltas and HTTP MCP over a remote worker.

Acceptance test for #441. One API-only app serves HTTP while a
credential-less worker (no ``DATABASE_URL``, no object-store
credentials, ``store=None``) connects over ``/internal/worker`` through
the real worker protocol and runs turns from the command context. The
``FakeWorker`` socket plus ``dispatch_command`` is the closest existing
pattern to a separate worker process. A streaming fake model host emits
several text chunks per turn, and the agent carries an HTTP MCP tool
whose bearer comes from the vault.

The test asserts that the SSE client sees multiple
``agent.session.turn.output_text.delta`` events before
``output_text.done`` with the concatenated deltas equal to the final
text, that an MCP call item with ``server_label`` and the tool name is
stored on two consecutive turns, and that deltas are never stored
(``after_seq`` replay and export are unchanged). With a Postgres test
database a second API replica streams the same deltas over the shared
event bus.
"""

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker

from apipi.api.sessions import _event_stream
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.event_bus import InMemoryEventBus
from apipi.services.runtime import FakeHarness, usage_from
from apipi.store.engine import Store
from apipi.worker.artifact_upload import handle_presign_reply
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
from apipi.worker.execution import RemoteExecution, local_execution
from apipi.worker.hub import dispatch_command
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


class StreamingMcpHarness(FakeHarness):
    """Fake model host: spaced text chunks plus scripted MCP calls."""

    def __init__(self, chunks: list[str]) -> None:
        super().__init__()
        self.chunks = list(chunks)
        self.mcp_http_turns: list[Any] = []

    async def generate(self, text: str, **kwargs: object):  # type: ignore[override]
        raw_mcp = kwargs.get("mcp_http")
        self.mcp_http = list(raw_mcp) if isinstance(raw_mcp, list) else None
        self.mcp_http_turns.append(self.mcp_http)
        for call in self.mcp_calls:
            assert isinstance(call, dict)
            yield (
                "agent.session.turn.item.added",
                {
                    "item_type": "mcp_call",
                    "call_id": call.get("call_id"),
                    "name": call.get("name"),
                    "server_label": call.get("server_label"),
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


async def test_split_mode_deltas_and_http_mcp(
    settings: Settings,
    store: Store,
    worker_secret: str,
    mcp_server: tuple[str, dict[str, str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_url, _seen = mcp_server
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert os.environ.get("DATABASE_URL") is None
    api_settings = _api_settings(settings)
    app = create_app(api_settings, store=store, harness=FakeHarness())
    assert isinstance(app.state.execution, RemoteExecution)

    worker_settings = Settings(
        run_mode="none",
        sessions_dir=str(tmp_path / "worker-sessions"),
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
    send_lock = asyncio.Lock()
    socket_deltas: list[dict[str, Any]] = []

    async def sock_send(payload: dict[str, Any]) -> None:
        if payload.get("type") == "delta.text":
            socket_deltas.append(payload)
        async with send_lock:
            await worker.send_json(payload)

    relay.attach(sock_send)
    stop = asyncio.Event()
    ready = asyncio.Event()
    command_tasks: set[asyncio.Task[None]] = set()

    async def run_worker() -> None:
        hello = await worker.connect(capacity=2)
        assert hello.get("ok") is True
        ready.set()
        while not stop.is_set():
            try:
                message = await worker.receive_json(timeout=5)
            except TimeoutError:
                continue
            kind = message.get("type")
            if kind == "command":
                await sock_send(
                    {
                        "type": "lease.ack",
                        "id": message.get("id"),
                        "lease_id": message.get("lease_id"),
                    }
                )
                task = asyncio.create_task(dispatch_command(execution, message))
                command_tasks.add(task)
            elif kind == "ack":
                try:
                    outbox.acked(
                        uuid.UUID(str(message.get("session_id"))),
                        int(str(message.get("last_seq"))),
                    )
                except (ValueError, TypeError):
                    continue
            elif kind == "artifact.presign.reply":
                handle_presign_reply(execution.presign_waiters, message)

    async def pump_outbox() -> None:
        sent: set[tuple[uuid.UUID, int]] = set()
        while not stop.is_set():
            for session_id in outbox.pending_sessions():
                for envelope in outbox.pending(session_id):
                    try:
                        key = (session_id, int(envelope["seq"]))
                    except (KeyError, ValueError, TypeError):
                        continue
                    if key in sent:
                        continue
                    sent.add(key)
                    await sock_send(dict(envelope))
            await asyncio.sleep(0.005)

    token = "split-441"
    tenant_id = _tenant(token)
    worker_task = asyncio.create_task(run_worker())
    pump_task = asyncio.create_task(pump_outbox())
    await ready.wait()
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
                second_app = create_app(
                    api_settings, store=store, harness=FakeHarness()
                )
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
            assert socket_deltas, "worker sent no delta.text envelopes"
            assert "".join(
                str(item["payload"]["text"]) for item in socket_deltas
            ).startswith(TEXT_FIRST)
            assert harness.mcp_http_turns, "worker harness saw no turn"
            first_mcp = harness.mcp_http_turns[0]
            assert first_mcp is not None and first_mcp[0].server_label == "mock"
            assert first_mcp[0].headers == {"Authorization": "Bearer vault-secret"}

            harness.chunks = list(CHUNKS_SECOND)
            harness.mcp_calls = [
                {"call_id": "call-2", "name": "mock_tool", "server_label": "mock"}
            ]
            before = len(socket_deltas)
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
            assert len(socket_deltas) > before
            assert "".join(str(item["payload"]["text"]) for item in socket_deltas) == (
                TEXT_FIRST + TEXT_SECOND
            )
            assert len(harness.mcp_http_turns) >= 2
            second_mcp = harness.mcp_http_turns[-1]
            assert second_mcp is not None and second_mcp[0].server_label == "mock"
            assert second_mcp[0].headers == {"Authorization": "Bearer vault-secret"}

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
        stop.set()
        worker_task.cancel()
        pump_task.cancel()
        for task in command_tasks:
            if not task.done():
                task.cancel()
        relay.detach()
        await worker.close()
        await execution.close()
        await bus.close()
