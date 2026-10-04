import base64
import os
import uuid
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for, split_client_for
from tests.unit.test_blobs import FakeS3

from apipi.common.objects import NS_FILES
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.protocol import (
    MAX_COMMAND_BYTES,
    TurnStartCommandPayload,
    dumps_wire,
    parse_turn_context,
    strict_parse,
    wire_bytes,
)
from apipi.store.blobs import S3Blobs, S3Store, file_object_id
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness

_REGISTRY = {"test": {"input": ["text", "image"], "reasoning": True}}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid5(NAMESPACE_URL, hash_token(token))


def _vision(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"model_registry": _REGISTRY, **update})


def _png(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + os.urandom(size - 8)


def _data_url(data: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(data).decode()}"


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": list(parts)}],
            }
        ]
    }


async def _session(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def _upload(
    client: AsyncClient, token: str, data: bytes, content_type: str = "image/png"
) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": ("photo", data, content_type)},
    )
    assert uploaded.status_code == 200
    return str(uploaded.json()["id"])


async def _user_content(client: AsyncClient, token: str, session_id: str) -> Any:
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    assert items.status_code == 200
    users = [item for item in items.json()["data"] if item["data"]["role"] == "user"]
    return users[-1]["data"]["content"]


def _spy_commands(app: Any) -> list[dict[str, Any]]:
    hub = app.state.workers
    real = hub._send
    sent: list[dict[str, Any]] = []

    async def _send(conn: Any, wire: dict[str, Any]) -> None:
        if wire.get("type") == "command":
            sent.append(wire)
        await real(conn, wire)

    hub._send = _send
    return sent


async def test_image_over_the_command_limit_reaches_pi_as_a_reference(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    image = _png(300 * 1024)
    encoded = base64.b64encode(image).decode()
    async with split_client_for(
        _vision(settings), store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        commands = _spy_commands(app)
        token = "image-large"
        session_id = await _session(client, token)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(
                {"type": "input_text", "text": "see"},
                {"type": "input_image", "image_url": _data_url(image)},
            ),
        )
        assert sent.status_code == 200, sent.json()
        content = await _user_content(client, token, session_id)
        file_id = content[1]["file_id"]
        stored = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    assert harness.images == [
        [{"type": "image", "data": encoded, "mimeType": "image/png"}]
    ]
    assert content == [
        {"type": "input_text", "text": "see"},
        {"type": "input_image", "file_id": file_id},
    ]
    assert stored.content == image
    start = next(wire for wire in commands if wire["op"] == "turn.start")
    text = dumps_wire(start)
    assert encoded[:4096] not in text
    assert wire_bytes(text) < 64 * 1024 < len(image)
    payload = start["payload"]
    parse_turn_context(payload["context"])
    assert payload["images"] == []
    assert payload["parts"][0] == {"type": "input_text", "text": "see"}
    ref = payload["parts"][1]
    assert ref == {
        "type": "image",
        "file_id": file_id,
        "object_id": file_object_id(_tenant(token), file_id),
        "local_path": ref["local_path"],
        "mime_type": "image/png",
        "size_bytes": len(image),
    }
    assert not os.path.isabs(ref["local_path"])


async def test_max_images_at_the_size_limit_start_one_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    limit = 300 * 1024
    vision = _vision(
        settings,
        max_images=8,
        max_image_bytes=limit,
        max_request_bytes=8 * 1024 * 1024,
    )
    images = [_png(limit) for _ in range(8)]
    harness = FakeHarness()
    async with split_client_for(
        vision, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "image-many"
        session_id = await _session(client, token)
        parts = [
            {"type": "input_image", "image_url": _data_url(data)} for data in images
        ]
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message({"type": "input_text", "text": "all"}, *parts),
        )
        assert sent.status_code == 200, sent.json()
        content = await _user_content(client, token, session_id)
        too_many = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(*parts, parts[0]),
        )
    assert [part["type"] for part in content] == ["input_text"] + ["input_image"] * 8
    assert len({part["file_id"] for part in content[1:]}) == 8
    assert [base64.b64decode(part["data"]) for part in harness.images[0]] == images
    assert too_many.status_code == 400


async def test_input_image_by_file_id(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    limit = 64 * 1024
    harness = FakeHarness()
    image = _png(limit)
    async with split_client_for(
        _vision(settings, max_image_bytes=limit),
        store,
        harness=harness,
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "image-file-id"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, image)
        foreign = await _upload(client, "image-other-tenant", image)
        text_file = await _upload(client, token, b"plain words", "text/plain")
        big_file = await _upload(client, token, _png(limit + 1))
        path = f"/v1/agents/sessions/{session_id}/events"
        sent = await client.post(
            path,
            headers=_auth(token),
            json=_message(
                {"type": "input_image", "file_id": file_id, "detail": "high"},
                {"type": "input_text", "text": "what is this"},
            ),
        )
        assert sent.status_code == 200, sent.json()
        content = await _user_content(client, token, session_id)
        listed = await client.get("/v1/files", headers=_auth(token))
        missing = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_image", "file_id": foreign}),
        )
        not_image = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_image", "file_id": text_file}),
        )
        too_big = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_image", "file_id": big_file}),
        )
        neither = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_image", "detail": "low"}),
        )
    assert content == [
        {"type": "input_image", "file_id": file_id},
        {"type": "input_text", "text": "what is this"},
    ]
    assert len(listed.json()["data"]) == 3
    assert harness.images == [
        [
            {
                "type": "image",
                "data": base64.b64encode(image).decode(),
                "mimeType": "image/png",
            }
        ]
    ]
    assert missing.status_code == 404
    assert not_image.status_code == 400
    assert not_image.json()["error"]["code"] == "invalid_request"
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    assert neither.status_code == 400


async def test_session_create_with_a_foreign_file_id_creates_no_session(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(_vision(settings), store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        foreign = await _upload(client, "image-owner", _png(1024))
        token = "image-create"
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "input": {
                    "role": "user",
                    "content": [{"type": "input_image", "file_id": foreign}],
                },
            },
        )
        sessions = await client.get("/v1/agents/sessions", headers=_auth(token))
    assert created.status_code == 404
    assert sessions.json()["data"] == []


async def test_command_over_the_limit_fails_with_a_clear_message(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "image-text-too-long"
        session_id = await _session(client, token)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={
                "type": "agent.session.input.message",
                "content": "x" * (MAX_COMMAND_BYTES + 1),
            },
        )
    assert sent.status_code == 413
    error = sent.json()["error"]
    assert error["code"] == "payload_too_large"
    assert "one turn can carry" in error["message"]


async def test_worker_without_image_refs_gets_no_image_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(_vision(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect(features=["lease_cursor", "presign", "search"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "image-old-worker"
        session_id = await _session(client, token)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message({"type": "input_image", "image_url": _data_url(_png(64))}),
        )
    await worker.close()
    assert sent.status_code == 501
    assert sent.json()["error"]["code"] == "unsupported_op"
    assert "image_refs" in sent.json()["error"]["message"]


async def test_s3_store_sends_presigned_image_refs(
    settings: Settings,
    store: Store,
    worker_secret: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s3_settings = _vision(
        Settings(
            database_url=settings.database_url,
            run_mode="none",
            sessions_dir=settings.sessions_dir,
            artifact_store="s3",
            s3_bucket="bucket",
            s3_prefix="apipi/artifacts",
            s3_endpoint="https://s3.example",
            s3_region="us-east-1",
        )
    )
    fake = FakeS3()
    fetched: list[dict[str, Any]] = []

    async def _fetch(ref: Any, settings: Settings) -> bytes:
        del settings
        fetched.append(dict(ref))
        data = fake.objects.get(urlparse(ref["url"]).path.lstrip("/"))
        assert data is not None
        return data

    monkeypatch.setattr("apipi.worker.turn_context.fetch_ref_bytes", _fetch)
    harness = FakeHarness()
    image = _png(300 * 1024)
    async with split_client_for(
        s3_settings,
        store,
        harness=harness,
        token=worker_secret,
        blobs=S3Blobs(s3_settings, client=fake),
        objects=S3Store(s3_settings, client=fake),
    ) as (_app, client, _worker):
        token = "image-s3"
        session_id = await _session(client, token)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message({"type": "input_image", "image_url": _data_url(image)}),
        )
        assert sent.status_code == 200, sent.json()
        content = await _user_content(client, token, session_id)
    file_id = content[0]["file_id"]
    object_id = file_object_id(_tenant(token), file_id)
    stored = await S3Store(s3_settings, client=fake).get(NS_FILES, object_id)
    assert stored == image
    assert fetched[0]["object_id"] == object_id
    assert fetched[0]["url"].startswith("https://bucket.example/")
    assert "local_path" not in fetched[0]
    assert base64.b64decode(harness.images[0][0]["data"]) == image


def test_image_part_on_the_wire_has_no_bytes() -> None:
    ref = {
        "type": "image",
        "file_id": "file-1",
        "object_id": "files/t/file-1",
        "local_path": "files/t/file-1",
        "mime_type": "image/png",
        "size_bytes": 3,
    }
    with strict_parse(False):
        body = TurnStartCommandPayload.model_validate(
            {"parts": [{**ref, "data": "AAAA", "mimeType": "image/png"}]}
        )
    assert body.to_wire()["parts"] == [ref]
    with pytest.raises(ValueError):
        TurnStartCommandPayload.model_validate({"parts": [{**ref, "data": "AAAA"}]})
