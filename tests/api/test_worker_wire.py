import asyncio
import uuid
from typing import Any

from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.protocol import (
    CumulativeAck,
    HelloReply,
    InventoryReply,
    LeaseRevoke,
    TurnStartCommandPayload,
    WorkerCommand,
    parse_api_message,
)
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def _worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
    )


async def _session(client: AsyncClient, token: str) -> uuid.UUID:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    return uuid.UUID(created.json()["id"])


async def test_hello_and_inventory_reply_parse_as_models(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    ghost = uuid.uuid4()
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    welcome = HelloReply.model_validate(hello)
    assert welcome.worker_id is not None
    assert welcome.revoke == []
    await worker.send_json(
        {
            "type": "inventory",
            "sessions": [{"session_id": str(ghost), "last_seq": 0}],
        }
    )
    reply = await worker.receive_json()
    parsed = parse_api_message(reply)
    assert isinstance(parsed, InventoryReply)
    assert [entry.session_id for entry in parsed.revoke] == [ghost]
    assert parsed.revoke[0].lease_id is None
    assert reply["revoke"] == [{"type": "lease.revoke", "session_id": str(ghost)}]
    await worker.close()


async def test_heartbeat_with_every_worker_field_extends_leases(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect(capacity=2)
    await worker.send_json(
        {
            "type": "heartbeat",
            "capacity": 3,
            "memory_mb": 4096,
            "run_mode": "none",
            "accepts": ["none"],
            "arch": "x86_64",
            "image_store_version": "v1",
            "images": [],
            "drain": True,
        }
    )
    worker_id = uuid.UUID(str(worker.worker_id))
    for _ in range(100):
        conn = app.state.workers.get(worker_id)
        if conn is not None and conn.capacity == 3 and conn.draining:
            break
        await asyncio.sleep(0.02)
    conn = app.state.workers.get(worker_id)
    assert conn is not None
    assert conn.capacity == 3
    assert conn.memory_mb == 4096
    assert conn.draining is True
    await worker.close()


async def test_turn_command_carries_the_model_key_only_in_the_context(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_worker_settings(settings)), store=store)
    token = "wire"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _session(client, token)
        context = await app.state.sessions._turn_context(
            _tenant(token),
            session_id,
            [],
            api_key="model-key",
            key_id=None,
            user_id=None,
            org_id=None,
        )
        worker = FakeWorker(app, worker_secret)
        await worker.connect()
        run = asyncio.create_task(
            app.state.execution.run_turn(
                _tenant(token),
                session_id,
                "hi",
                request_id="req-1",
                api_key="model-key",
                key_id="key-1",
                user_id="user-1",
                org_id="org-1",
                turn_context=context,
            )
        )
        frame: dict[str, Any] = await worker.receive_json()
        run.cancel()
        await asyncio.gather(run, return_exceptions=True)
        await worker.close()
    parsed = parse_api_message(frame)
    assert isinstance(parsed, WorkerCommand)
    assert parsed.op == "turn.start"
    payload = parsed.parsed_payload()
    assert isinstance(payload, TurnStartCommandPayload)
    assert payload.text == "hi"
    assert payload.request_id == "req-1"
    assert payload.run_mode == "none"
    assert payload.last_seq == 0
    assert "api_key" not in frame["payload"]
    assert frame["payload"]["context"]["model"]["api_key"] == "model-key"
    assert payload.turn_context() is not None


async def test_acks_and_revokes_are_built_from_models() -> None:
    session_id = uuid.uuid4()
    ack = CumulativeAck(session_id=session_id, last_seq=3).to_wire()
    assert ack == {"type": "ack", "session_id": str(session_id), "last_seq": 3}
    lease_id = uuid.uuid4()
    revoke = LeaseRevoke(session_id=session_id, lease_id=lease_id).to_wire()
    assert revoke == {
        "type": "lease.revoke",
        "session_id": str(session_id),
        "lease_id": str(lease_id),
    }


async def test_every_worker_frame_of_a_turn_parses_as_a_model(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    import json

    from tests.support.split_worker import split_client_for, wait_for_event_types

    from apipi.protocol import (
        DURABLE_MESSAGE_TYPES,
        WorkerEnvelope,
        parse_envelope,
        parse_worker_message,
    )

    token = "wire-frames"
    sent: list[str] = []
    async with split_client_for(settings, store, token=worker_secret, sent=sent) as (
        _app,
        client,
        _worker,
    ):
        session_id = await _session(client, token)
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "text": "hi"},
        )
        assert posted.status_code == 200, posted.text
        await wait_for_event_types(
            client, token, str(session_id), "agent.session.turn.completed"
        )
    types: set[str] = set()
    for raw in sent:
        frame = json.loads(raw)
        if frame.get("v") == 2:
            envelope = parse_envelope(frame)
            assert isinstance(envelope, WorkerEnvelope)
            types.add(envelope.type)
        else:
            assert parse_worker_message(frame) is not None, frame
            types.add(frame["type"])
    assert {"register", "lease.ack", "turn.status", "usage"} <= types
    assert types & DURABLE_MESSAGE_TYPES
