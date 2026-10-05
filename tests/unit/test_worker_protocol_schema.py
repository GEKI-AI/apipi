import json
import uuid
from typing import Any

import pytest
from tests.support import conformance, wire_schema
from tests.support.http import auth
from tests.support.split_worker import split_client_for, wait_for_idle

from apipi.config import Settings
from apipi.protocol import (
    API_MESSAGE_MODELS,
    COMMAND_OPS,
    DURABLE_MESSAGE_TYPES,
    EPHEMERAL_MESSAGE_TYPES,
    WORKER_MESSAGE_MODELS,
)
from apipi.protocol.schema import (
    API_TO_WORKER,
    WORKER_TO_API,
    message_schemas,
    schema_name_for,
)
from apipi.store.engine import Store

REGENERATE = "run: uv run python scripts/gen_worker_schema.py"


def test_committed_schema_matches_the_models() -> None:
    generated = message_schemas()
    files = {path.stem for path in wire_schema.SCHEMA_DIR.glob("*.json")}
    assert files == set(generated), REGENERATE
    for name, schema in generated.items():
        committed = json.loads(
            (wire_schema.SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8")
        )
        assert committed == schema, f"{name}.json is stale, {REGENERATE}"


def test_every_message_has_a_schema() -> None:
    names = set(message_schemas())
    for kind in WORKER_MESSAGE_MODELS:
        assert kind in names
    for kind in API_MESSAGE_MODELS:
        assert kind in names
    for kind in DURABLE_MESSAGE_TYPES | EPHEMERAL_MESSAGE_TYPES:
        assert f"envelope.{kind}" in names
    for op in COMMAND_OPS:
        assert f"command.{op}" in names
    assert {"error", "context", "envelope", "index"} <= names


def test_frames_are_told_apart_by_v_op_and_type() -> None:
    assert schema_name_for(WORKER_TO_API, {"type": "event", "v": 2}) == "envelope.event"
    assert schema_name_for(WORKER_TO_API, {"type": "heartbeat"}) == "heartbeat"
    assert schema_name_for(WORKER_TO_API, {"type": "event"}) is None
    assert (
        schema_name_for(API_TO_WORKER, {"type": "command", "op": "turn.start"})
        == "command.turn.start"
    )
    assert schema_name_for(API_TO_WORKER, {"type": "command", "op": "nope"}) is None
    assert schema_name_for(API_TO_WORKER, {"type": "error"}) == "error"


def _id() -> str:
    return str(uuid.uuid4())


@pytest.mark.parametrize(
    ("direction", "frame", "ok"),
    [
        (WORKER_TO_API, {"type": "lease.ack", "id": _id(), "lease_id": _id()}, True),
        (WORKER_TO_API, {"type": "lease.ack", "id": "x", "lease_id": _id()}, False),
        (
            WORKER_TO_API,
            {"type": "lease.ack", "id": _id().upper(), "lease_id": _id()},
            False,
        ),
        (API_TO_WORKER, {"type": "ack", "session_id": _id(), "last_seq": "5"}, False),
        (
            API_TO_WORKER,
            {"type": "error", "ok": False, "error": "x", "code": "revoked"},
            True,
        ),
        (API_TO_WORKER, {"type": "error", "ok": False, "error": "x"}, False),
        (
            WORKER_TO_API,
            {
                "v": 2,
                "session_id": _id(),
                "seq": 1,
                "type": "session.status",
                "payload": {"status": "idle", "future_field": 1},
            },
            True,
        ),
    ],
)
def test_schema_accepts_and_rejects(
    direction: str, frame: dict[str, Any], ok: bool
) -> None:
    assert (wire_schema.frame_errors(direction, frame) == []) is ok


async def test_a_real_turn_is_captured_and_valid(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    token = "schema-capture"
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        headers = auth(token)
        agent = await client.post(
            "/v1/agents", headers=headers, json={"name": "bot", "model": "test"}
        )
        session = await client.post(
            "/v1/agents/sessions",
            headers=headers,
            json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
        )
        session_id = session.json()["id"]
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=headers,
            json={"type": "agent.session.input.message", "content": "hi"},
        )
        assert posted.status_code == 200
        await wait_for_idle(client, token, session_id)
    frames = wire_schema.captured or []
    seen = {(direction, json.loads(text).get("type")) for direction, text in frames}
    assert (WORKER_TO_API, "register") in seen
    assert (API_TO_WORKER, "hello") in seen
    assert (API_TO_WORKER, "command") in seen
    assert (WORKER_TO_API, "usage") in seen
    assert wire_schema.capture_errors(frames) == []


@pytest.mark.parametrize("name", conformance.names())
def test_every_frame_of_every_transcript_matches_the_schema(name: str) -> None:
    binds: dict[str, Any] = {
        key: _id() for key in ("session", "tenant", "lease", "worker", "turn")
    }
    errors: list[str] = []
    for step in conformance.load(name).steps:
        if not conformance.is_frame(step):
            continue
        frame = conformance.render(
            step["frame"], binds, store_check=lambda: ("." + "a" * 32, "b" * 32)
        )
        direction = WORKER_TO_API if step["from"] == "worker" else API_TO_WORKER
        errors.extend(wire_schema.frame_errors(direction, frame))
    assert errors == []
