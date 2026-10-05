import base64
import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.files import input_file, message, spy_commands, vision
from tests.support.split_worker import api_settings_for, split_client_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.protocol import SUPPORTED_FEATURES, dumps_wire
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PNG = b"\x89PNG\r\n\x1a\n"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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


async def test_text_files_reach_the_model_with_their_names(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        commands = spy_commands(app)
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
            json=message(
                {"type": "input_text", "text": "read these"},
                input_file(notes),
                input_file(table, filename="sales.csv"),
                input_file(data),
                input_file(config),
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


async def test_session_create_with_only_a_text_file_starts_a_turn(
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
                "input": {"role": "user", "content": [input_file(file_id)]},
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
    image = _PNG + os.urandom(1024)
    async with split_client_for(
        vision(settings), store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "file-image"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, image, "shot.png", "image/png")
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=message({"type": "input_text", "text": "look"}, input_file(file_id)),
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
        file_id = await _upload(client, token, _PNG, "a.png", "image/png")
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=message(input_file(file_id)),
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
                path, headers=_auth(token), json=message(input_file(file_id))
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
        ok = await client.post(
            path, headers=_auth(token), json=message(input_file(exact))
        )
    for name in ("xlsx", "pdf", "latin", "unknown_ext"):
        assert results[name].status_code == 400, name
        assert results[name].json()["error"]["code"] == "unsupported_file_type"
    assert "needs a computer" in results["xlsx"].json()["error"]["message"]
    assert results["big"].status_code == 413
    assert results["big"].json()["error"]["code"] == "payload_too_large"
    assert results["foreign"].status_code == 404
    assert ok.status_code == 200, ok.json()
    assert harness.prompts == [f'<file name="exact.txt">\n{"y" * limit}\n</file>']


async def test_invalid_input_parts_fail_before_the_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        vision(settings, max_files_per_message=2),
        store,
        harness=harness,
        token=worker_secret,
    ) as (_app, client, _worker):
        token = "file-parts"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"a", "a.txt", "text/plain")
        png_id = await _upload(client, token, _PNG, "a.png", "image/png")
        image_url = f"data:image/png;base64,{base64.b64encode(_PNG).decode()}"
        image = {"type": "input_image", "file_id": png_id}
        one_source = "input_image needs image_url or file_id"
        cases: list[tuple[list[dict[str, Any]], str, str]] = [
            (
                [{"type": "input_text", "text": "x", "file_id": file_id}],
                "unknown_field",
                "Unknown field: file_id",
            ),
            ([{**image, "text": "x"}], "unknown_field", "Unknown field: text"),
            (
                [{"type": "input_audio", "data": "x"}],
                "input_audio",
                "input_audio is not implemented",
            ),
            ([{"type": "input_image", "detail": "low"}], "invalid_request", one_source),
            ([{**image, "image_url": image_url}], "invalid_request", one_source),
            ([{"type": "input_file"}], "invalid_request", "input_file needs file_id"),
            (
                [{"type": "input_file", "file_data": "data:text/plain,a"}],
                "file_data",
                "input_file file_data is not implemented",
            ),
            (
                [input_file(file_id, file_url="https://example.com/a.txt")],
                "file_url",
                "input_file file_url is not implemented",
            ),
            (
                [input_file(file_id, detail="high")],
                "unknown_field",
                "Unknown field: detail",
            ),
            ([input_file(file_id)] * 3, "invalid_request", "at most 2 input_file"),
        ]
        path = f"/v1/agents/sessions/{session_id}/events"
        failed = [
            await client.post(path, headers=_auth(token), json=message(*parts))
            for parts, _code, _message in cases
        ]
        two = await client.post(
            path,
            headers=_auth(token),
            json=message(input_file(file_id), input_file(file_id)),
        )
    for (parts, code, text), response in zip(cases, failed, strict=True):
        assert response.status_code == 400, (parts, response.json())
        error = response.json()["error"]
        assert error["code"] == code, (parts, error)
        assert text in error["message"], (parts, error)
    assert two.status_code == 200, two.json()
    assert len(harness.prompts) == 1


@pytest.mark.parametrize(
    ("feature", "environment", "part_type"),
    [
        ("file_refs", "none", "input_file"),
        ("session_files", "openai_hosted", "input_file"),
        ("image_refs", "none", "input_image"),
    ],
)
async def test_worker_without_the_feature_gets_no_turn(
    settings: Settings,
    store: Store,
    worker_secret: str,
    feature: str,
    environment: str,
    part_type: str,
) -> None:
    app = create_app(api_settings_for(vision(settings)), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect(
        accepts=["none", "microvm"],
        features=sorted(SUPPORTED_FEATURES - {feature}),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "file-old-worker"
        uploaded: list[str] = []
        if part_type == "input_file":
            uploaded.append(await _upload(client, token, b"a", "a.txt", "text/plain"))
            part = input_file(uploaded[0])
        else:
            image = base64.b64encode(_PNG + os.urandom(56)).decode()
            part = {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{image}",
            }
        session_id = await _session(client, token, environment)
        sent = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=message(part),
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": await _agent(client, token),
                "environment": {"type": environment},
                "input": {"role": "user", "content": [part]},
            },
        )
        files = await client.get("/v1/apipi/files", headers=_auth(token))
    await worker.close()
    assert sent.status_code == 501
    assert sent.json()["error"]["code"] == "unsupported_op"
    assert feature in sent.json()["error"]["message"]
    assert created.status_code == 501
    assert feature in created.json()["error"]["message"]
    assert [row["id"] for row in files.json()["data"]] == uploaded
