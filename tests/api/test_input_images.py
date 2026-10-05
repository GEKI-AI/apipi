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
from apipi.gateway.auth import AuthRequest, tenant_from_key
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


class _Users:
    def __call__(self, token: str, request: AuthRequest) -> dict[str, object]:
        user = request.headers.get("x-end-user")
        return {
            "key_id": hash_token(token),
            "tenant_id": tenant_from_key(token),
            "user_id": user,
            "cache_key": f"{token}:{user}",
        }

    def cache_key(self, token: str, request: AuthRequest) -> str:
        return f"{token}:{request.headers.get('x-end-user')}"


async def test_input_image_of_another_user_is_not_found(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        _vision(settings),
        store,
        harness=harness,
        token=worker_secret,
        authenticate=_Users(),
    ) as (_app, client, _worker):
        token = "image-users"
        u1 = {**_auth(token), "X-End-User": "u1"}
        u2 = {**_auth(token), "X-End-User": "u2"}
        uploaded = await client.post(
            "/v1/files",
            headers=u1,
            data={"purpose": "vision"},
            files={"file": ("photo.png", _png(1024), "image/png")},
        )
        assert uploaded.status_code == 200, uploaded.json()
        image = uploaded.json()["id"]
        agent = await client.post(
            "/v1/agents", headers=u1, json={"name": "bot", "model": "test"}
        )
        agent_id = agent.json()["id"]
        part = {"type": "input_image", "file_id": image}
        u2_create = await client.post(
            "/v1/agents/sessions",
            headers=u2,
            json={
                "agent_id": agent_id,
                "environment": {"type": "none"},
                "input": {"role": "user", "content": [part]},
            },
        )
        u2_sessions = await client.get("/v1/agents/sessions", headers=u2)
        sessions: dict[str, str] = {}
        for name, headers in (("u1", u1), ("u2", u2), ("none", _auth(token))):
            created = await client.post(
                "/v1/agents/sessions",
                headers=headers,
                json={"agent_id": agent_id, "environment": {"type": "none"}},
            )
            assert created.status_code == 200, created.json()
            sessions[name] = created.json()["id"]
        u2_sent = await client.post(
            f"/v1/agents/sessions/{sessions['u2']}/events",
            headers=u2,
            json=_message(part),
        )
        u1_sent = await client.post(
            f"/v1/agents/sessions/{sessions['u1']}/events",
            headers=u1,
            json=_message(part),
        )
        none_sent = await client.post(
            f"/v1/agents/sessions/{sessions['none']}/events",
            headers=_auth(token),
            json=_message(part),
        )
    assert u2_create.status_code == 404
    assert u2_sessions.json()["data"] == []
    assert u2_sent.status_code == 404
    assert u1_sent.status_code == 200, u1_sent.json()
    assert none_sent.status_code == 200, none_sent.json()
    assert len(harness.images) == 2


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
        image = {"type": "input_image", "image_url": _data_url(_png(64))}
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(image),
        )
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": {"role": "user", "content": [image]},
            },
        )
        files = await client.get("/v1/files", headers=_auth(token))
    await worker.close()
    assert sent.status_code == 501
    assert sent.json()["error"]["code"] == "unsupported_op"
    assert "image_refs" in sent.json()["error"]["message"]
    assert created.status_code == 501
    assert files.json()["data"] == []


async def test_placement_prefers_a_worker_with_image_refs(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    from apipi.services.worker_tokens import create_token

    old_token = await create_token(store, name="old-worker")
    harness = FakeHarness()
    async with split_client_for(
        _vision(settings), store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        old = FakeWorker(app, old_token.secret)
        await old.connect(
            capacity=8,
            memory_mb=1_000_000,
            features=["lease_cursor", "presign", "search"],
        )
        token = "image-mixed-fleet"
        session_id = await _session(client, token)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message({"type": "input_image", "image_url": _data_url(_png(64))}),
        )
        await old.close()
    assert sent.status_code == 200, sent.json()
    assert len(harness.images) == 1


async def test_parts_reject_fields_of_the_other_part_type(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(_vision(settings), store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        token = "image-strict-parts"
        session_id = await _session(client, token)
        path = f"/v1/agents/sessions/{session_id}/events"
        text_with_file = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_text", "text": "x", "file_id": "file-1"}),
        )
        image_with_text = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_image", "file_id": "file-1", "text": "x"}),
        )
        unknown = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_audio", "data": "x"}),
        )
    for response in (text_with_file, image_with_text):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_field"
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "input_audio"


async def test_worker_checks_the_image_size_and_hides_the_url(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from apipi.common.dirs import store_root
    from apipi.common.errors import ObjectStoreError
    from apipi.worker.turn_context import fetch_input_images

    root = store_root(settings)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "files" / "image").write_bytes(b"12345")
    ref = {
        "type": "image",
        "file_id": "file-1",
        "object_id": "files/image",
        "local_path": "files/image",
        "mime_type": "image/png",
        "size_bytes": 4,
    }
    with pytest.raises(ObjectStoreError, match="larger than 4 bytes"):
        await fetch_input_images([ref], settings)
    with pytest.raises(ObjectStoreError, match="expected 6"):
        await fetch_input_images([{**ref, "size_bytes": 6}], settings)
    assert (await fetch_input_images([{**ref, "size_bytes": 5}], settings))[0][
        "data"
    ] == base64.b64encode(b"12345").decode()

    class _Broken:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def stream(self, method: str, url: str) -> Any:
            raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(httpx, "AsyncClient", _Broken)
    url = "https://bucket.example/files/image?X-Amz-Signature=secret"
    with pytest.raises(ObjectStoreError) as caught:
        await fetch_input_images([{**ref, "url": url, "local_path": None}], settings)
    assert "secret" not in str(caught.value)
    assert "secret" not in caught.value.key


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
