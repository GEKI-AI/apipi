import base64
import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for, split_client_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthRequest, tenant_from_key
from apipi.gateway.content import file_model_input
from apipi.gateway.tokens import hash_token
from apipi.protocol import TurnStartCommandPayload, dumps_wire
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.turn_context import fetch_input_files, file_block

_REGISTRY = {"test": {"input": ["text", "image"], "reasoning": True}}
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _vision(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"model_registry": _REGISTRY, **update})


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": list(parts)}],
            }
        ]
    }


def _file(file_id: str, **extra: Any) -> dict[str, Any]:
    return {"type": "input_file", "file_id": file_id, **extra}


async def _agent(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    return str(agent.json()["id"])


async def _session(client: AsyncClient, token: str, environment: str = "none") -> str:
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": await _agent(client, token),
            "environment": {"type": environment},
        },
    )
    assert created.status_code == 200, created.json()
    return str(created.json()["id"])


async def _upload(
    client: AsyncClient,
    token: str,
    data: bytes,
    filename: str,
    content_type: str,
) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": (filename, data, content_type)},
    )
    assert uploaded.status_code == 200, uploaded.json()
    return str(uploaded.json()["id"])


async def _user_item(client: AsyncClient, token: str, session_id: str) -> Any:
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    assert items.status_code == 200
    users = [item for item in items.json()["data"] if item["data"]["role"] == "user"]
    return users[-1]


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


async def test_text_files_reach_the_model_with_their_names(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        commands = _spy_commands(app)
        token = "file-text"
        session_id = await _session(client, token)
        notes = await _upload(
            client, token, b"# Plan\nship it\n", "notes.md", "text/markdown"
        )
        table = await _upload(client, token, b"a,b\n1,2\n", "table.csv", "text/csv")
        data = await _upload(
            client, token, b'{"k": "v"}', "data.json", "application/json"
        )
        config = await _upload(
            client, token, b"key: value", "config.yml", "application/octet-stream"
        )
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(
                {"type": "input_text", "text": "read these"},
                _file(notes),
                _file(table, filename="sales.csv"),
                _file(data),
                _file(config),
            ),
        )
        assert sent.status_code == 200, sent.json()
        item = await _user_item(client, token, session_id)
        bound = await client.get(
            f"/v1/apipi/sessions/{session_id}/files?order=asc", headers=_auth(token)
        )
        kinds = await client.get(
            f"/v1/apipi/files?session_id={session_id}", headers=_auth(token)
        )
    assert harness.prompts == [
        "read these\n"
        '<file name="notes.md">\n# Plan\nship it\n</file>\n'
        '<file name="sales.csv">\na,b\n1,2\n</file>\n'
        '<file name="data.json">\n{"k": "v"}\n</file>\n'
        '<file name="config.yml">\nkey: value\n</file>'
    ]
    assert harness.images == []
    assert item["data"]["content"] == [
        {"type": "input_text", "text": "read these"},
        {"type": "input_file", "file_id": notes, "filename": "notes.md"},
        {"type": "input_file", "file_id": table, "filename": "sales.csv"},
        {"type": "input_file", "file_id": data, "filename": "data.json"},
        {"type": "input_file", "file_id": config, "filename": "config.yml"},
    ]
    listed = bound.json()["data"]
    assert [entry["file_id"] for entry in listed] == [notes, table, data, config]
    assert {entry["path"] for entry in listed} == {None}
    assert {entry["item_id"] for entry in listed} == {item["id"]}
    assert {entry["kind"] for entry in kinds.json()["data"]} == {"file"}
    start = next(wire for wire in commands if wire["op"] == "turn.start")
    assert "ship it" not in dumps_wire(start)
    parts = start["payload"]["parts"]
    assert parts[0] == {"type": "input_text", "text": "read these"}
    assert parts[1] == {
        "type": "file",
        "file_id": notes,
        "filename": "notes.md",
        "object_id": parts[1]["object_id"],
        "local_path": parts[1]["local_path"],
        "mime_type": "text/markdown",
        "size_bytes": 15,
        "model_input": "text",
    }
    assert parts[4]["mime_type"] == "application/octet-stream"


async def test_file_only_message_and_session_create_start_a_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "file-create"
        file_id = await _upload(client, token, b"hello", "a.txt", "text/plain")
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": await _agent(client, token),
                "environment": {"type": "none"},
                "input": {"role": "user", "content": [_file(file_id)]},
            },
        )
        assert created.status_code == 200, created.json()
        session_id = created.json()["id"]
        item = await _user_item(client, token, session_id)
        bound = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=_auth(token)
        )
    assert harness.prompts == ['<file name="a.txt">\nhello\n</file>']
    assert item["data"]["content"] == [
        {"type": "input_file", "file_id": file_id, "filename": "a.txt"}
    ]
    assert [entry["file_id"] for entry in bound.json()["data"]] == [file_id]


async def test_image_file_reaches_a_vision_model_as_an_image(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    image = b"\x89PNG\r\n\x1a\n" + os.urandom(1024)
    async with split_client_for(
        _vision(settings), store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "file-image"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, image, "shot.png", "image/png")
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message({"type": "input_text", "text": "look"}, _file(file_id)),
        )
        assert sent.status_code == 200, sent.json()
        item = await _user_item(client, token, session_id)
    assert harness.images == [
        [
            {
                "type": "image",
                "data": base64.b64encode(image).decode(),
                "mimeType": "image/png",
            }
        ]
    ]
    assert harness.prompts == ["look"]
    assert item["data"]["content"] == [
        {"type": "input_text", "text": "look"},
        {"type": "input_file", "file_id": file_id, "filename": "shot.png"},
    ]


async def test_image_file_to_a_model_without_images_is_unsupported_input(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "file-image-text-model"
        session_id = await _session(client, token)
        file_id = await _upload(
            client, token, b"\x89PNG\r\n\x1a\n", "a.png", "image/png"
        )
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(_file(file_id)),
        )
    assert sent.status_code == 400
    assert sent.json()["error"]["code"] == "unsupported_input"
    assert harness.prompts == []


async def test_files_the_model_cannot_read_fail_before_the_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    limit = 1024
    async with split_client_for(
        settings.model_copy(update={"max_inline_file_bytes": limit}),
        store,
        harness=harness,
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "file-unsupported"
        session_id = await _session(client, token)
        xlsx = await _upload(client, token, b"PK\x03\x04", "report.xlsx", _XLSX)
        pdf = await _upload(client, token, b"%PDF-1.7", "doc.pdf", "application/pdf")
        latin = await _upload(
            client, token, "café".encode("latin-1"), "menu.txt", "text/plain"
        )
        big = await _upload(client, token, b"x" * (limit + 1), "big.txt", "text/plain")
        exact = await _upload(client, token, b"y" * limit, "exact.txt", "text/plain")
        unknown_ext = await _upload(
            client, token, b"text", "blob.bin", "application/octet-stream"
        )
        foreign = await _upload(client, "file-other", b"x", "x.txt", "text/plain")
        path = f"/v1/agents/sessions/{session_id}/events"
        results = {
            name: await client.post(
                path, headers=_auth(token), json=_message(_file(file_id))
            )
            for name, file_id in {
                "xlsx": xlsx,
                "pdf": pdf,
                "latin": latin,
                "big": big,
                "unknown_ext": unknown_ext,
                "foreign": foreign,
            }.items()
        }
        ok = await client.post(path, headers=_auth(token), json=_message(_file(exact)))
    for name in ("xlsx", "pdf", "latin", "unknown_ext"):
        assert results[name].status_code == 400, name
        assert results[name].json()["error"]["code"] == "unsupported_file_type"
    assert "needs a computer" in results["xlsx"].json()["error"]["message"]
    assert results["big"].status_code == 413
    assert results["big"].json()["error"]["code"] == "payload_too_large"
    assert results["foreign"].status_code == 404
    assert ok.status_code == 200, ok.json()
    assert harness.prompts == [f'<file name="exact.txt">\n{"y" * limit}\n</file>']


async def test_input_file_part_validation_and_count_limit(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings.model_copy(update={"max_files_per_message": 2}),
        store,
        harness=harness,
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "file-parts"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"a", "a.txt", "text/plain")
        path = f"/v1/agents/sessions/{session_id}/events"
        no_id = await client.post(
            path, headers=_auth(token), json=_message({"type": "input_file"})
        )
        by_data = await client.post(
            path,
            headers=_auth(token),
            json=_message({"type": "input_file", "file_data": "data:text/plain,a"}),
        )
        by_url = await client.post(
            path,
            headers=_auth(token),
            json=_message(_file(file_id, file_url="https://example.com/a.txt")),
        )
        extra = await client.post(
            path, headers=_auth(token), json=_message(_file(file_id, detail="high"))
        )
        too_many = await client.post(
            path,
            headers=_auth(token),
            json=_message(_file(file_id), _file(file_id), _file(file_id)),
        )
        two = await client.post(
            path, headers=_auth(token), json=_message(_file(file_id), _file(file_id))
        )
    assert no_id.status_code == 400
    assert by_data.status_code == 400
    assert by_data.json()["error"]["code"] == "file_data"
    assert by_url.status_code == 400
    assert by_url.json()["error"]["code"] == "file_url"
    assert extra.status_code == 400
    assert extra.json()["error"]["code"] == "unknown_field"
    assert too_many.status_code == 400
    assert "at most 2" in too_many.json()["error"]["message"]
    assert two.status_code == 200, two.json()
    assert len(harness.prompts) == 1


async def test_session_with_a_computer_does_not_take_input_file_yet(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "file-hosted"
        file_id = await _upload(client, token, b"a", "a.txt", "text/plain")
        session_id = await _session(client, token, environment="openai_hosted")
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(_file(file_id)),
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": await _agent(client, token),
                "input": {"role": "user", "content": [_file(file_id)]},
            },
        )
        bound = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=_auth(token)
        )
    for response in (sent, created):
        assert response.status_code == 501
        error = response.json()["error"]
        assert error["type"] == "not_implemented"
        assert error["code"] == "input_file"
    assert bound.json()["data"] == []
    assert harness.prompts == []


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


async def test_attachment_of_another_user_is_not_found(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings,
        store,
        harness=harness,
        token=worker_secret,
        authenticate=_Users(),
    ) as (app, client, _worker):
        token = "file-users"
        u1 = {**_auth(token), "X-End-User": "u1"}
        u2 = {**_auth(token), "X-End-User": "u2"}
        agent = await client.post(
            "/v1/agents", headers=u1, json={"name": "bot", "model": "test"}
        )
        agent_id = agent.json()["id"]
        created = await app.state.gateway.files.create(
            tenant_from_key(token),
            data=b"secret plan",
            filename="plan.md",
            purpose="user_data",
            content_type="text/markdown",
            kind="attachment",
            user_id="u1",
        )
        part = _file(str(created["id"]))
        u2_create = await client.post(
            "/v1/agents/sessions",
            headers=u2,
            json={
                "agent_id": agent_id,
                "environment": {"type": "none"},
                "input": {"role": "user", "content": [part]},
            },
        )
        sessions: dict[str, str] = {}
        for name, headers in (("u1", u1), ("u2", u2)):
            session = await client.post(
                "/v1/agents/sessions",
                headers=headers,
                json={"agent_id": agent_id, "environment": {"type": "none"}},
            )
            assert session.status_code == 200, session.json()
            sessions[name] = session.json()["id"]
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
        kinds = await client.get(
            "/v1/apipi/files", headers=u1, params={"kind": "attachment"}
        )
    assert u2_create.status_code == 404
    assert u2_sent.status_code == 404
    assert u1_sent.status_code == 200, u1_sent.json()
    assert harness.prompts == ['<file name="plan.md">\nsecret plan\n</file>']
    assert [row["id"] for row in kinds.json()["data"]] == [created["id"]]


async def test_worker_without_file_refs_gets_no_file_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(settings), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect(
        features=["image_refs", "lease_cursor", "presign", "search", "session_stopped"]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "file-old-worker"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"a", "a.txt", "text/plain")
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(_file(file_id)),
        )
    await worker.close()
    assert sent.status_code == 501
    assert sent.json()["error"]["code"] == "unsupported_op"
    assert "file_refs" in sent.json()["error"]["message"]


@pytest.mark.parametrize(
    ("content_type", "filename", "expected"),
    [
        ("text/markdown", "x", "text"),
        ("text/plain; charset=utf-8", "x", "text"),
        ("application/json", "x", "text"),
        ("application/x-yaml", "x", "text"),
        ("application/javascript", "x", "text"),
        (None, "notes.MD", "text"),
        ("application/octet-stream", "run.sql", "text"),
        ("application/octet-stream", "data", None),
        ("application/pdf", "doc.txt", None),
        (_XLSX, "report.xlsx", None),
        ("image/png", "a.png", "image"),
        ("image/svg+xml", "a.svg", None),
    ],
)
def test_file_model_input(
    settings: Settings, content_type: str | None, filename: str, expected: str | None
) -> None:
    assert file_model_input(settings, content_type, filename) == expected


async def test_worker_decodes_text_files_into_blocks(settings: Settings) -> None:
    from apipi.common.dirs import store_root
    from apipi.common.errors import ObjectStoreError

    root = store_root(settings)
    (root / "files").mkdir(parents=True, exist_ok=True)
    (root / "files" / "a").write_bytes(b"\xef\xbb\xbfline\n")
    (root / "files" / "b").write_bytes(b"\xff\xfe")
    ref = {
        "type": "file",
        "file_id": "file-1",
        "filename": 'say "hi".txt',
        "object_id": "files/a",
        "local_path": "files/a",
        "mime_type": "text/plain",
        "size_bytes": 8,
        "model_input": "text",
    }
    image = {**ref, "model_input": "image", "local_path": "files/b"}
    assert await fetch_input_files([ref, image], settings) == [
        '<file name="say &quot;hi&quot;.txt">\nline\n</file>'
    ]
    with pytest.raises(ObjectStoreError, match="expected 9"):
        await fetch_input_files([{**ref, "size_bytes": 9}], settings)
    with pytest.raises(UnicodeDecodeError):
        await fetch_input_files(
            [{**ref, "local_path": "files/b", "size_bytes": 2}], settings
        )
    assert file_block("a.md", "x") == '<file name="a.md">\nx\n</file>'


def test_file_part_on_the_wire_has_no_bytes() -> None:
    ref = {
        "type": "file",
        "file_id": "file-1",
        "filename": "a.md",
        "object_id": "files/t/file-1",
        "local_path": "files/t/file-1",
        "mime_type": "text/markdown",
        "size_bytes": 3,
        "model_input": "text",
    }
    body = TurnStartCommandPayload.model_validate({"parts": [ref]})
    assert body.to_wire()["parts"] == [ref]
    with pytest.raises(ValueError):
        TurnStartCommandPayload.model_validate(
            {"parts": [{**ref, "model_input": "pdf"}]}
        )
