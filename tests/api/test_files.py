import base64
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tests.support.files import Users, message, vision
from tests.support.http import auth, create_agent
from tests.support.split_worker import split_client_for

from apipi.common.objects import NS_FILES
from apipi.config import Settings
from apipi.gateway.auth import AuthFilter, AuthIdentity, AuthReject, tenant_from_key
from apipi.store.blobs import file_object_id
from apipi.store.engine import Store
from apipi.store.models import FileRow, utc_now
from apipi.store.repo import get_file
from apipi.worker.fake_harness import FakeHarness


async def test_files_crud_and_session_attach(client: AsyncClient) -> None:
    token = "files-crud"
    uploaded = await client.post(
        "/v1/files",
        headers=auth(token),
        data={"purpose": "user_data"},
        files={"file": ("amounts.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    file_id = body["id"]
    assert file_id.startswith("file-")
    assert body["object"] == "file"
    assert body["bytes"] == 8
    assert body["filename"] == "amounts.csv"
    assert body["purpose"] == "user_data"
    assert body["status"] == "processed"
    assert isinstance(body["created_at"], int)
    listed = await client.get("/v1/files", headers=auth(token))
    assert listed.json()["object"] == "list"
    assert listed.json()["data"][0]["id"] == file_id
    got = await client.get(f"/v1/files/{file_id}", headers=auth(token))
    assert got.json()["id"] == file_id
    content = await client.get(f"/v1/files/{file_id}/content", headers=auth(token))
    assert content.status_code == 200
    assert content.content == b"a,b\n1,2\n"
    other = await client.get(f"/v1/files/{file_id}", headers=auth("other-tenant"))
    assert other.status_code == 404
    agent_id = await create_agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "file_id",
                        "file_id": file_id,
                        "path": "/workspace/amounts.csv",
                    }
                ],
            },
        },
    )
    assert created.status_code == 200
    env = created.json()["environment"]
    assert env["files"][0]["file_id"] == file_id
    from typing import Any, cast

    from httpx import ASGITransport
    from tests.support.workspace import hosted_dir

    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = cast(Any, transport.app)
    settings = app.state.gateway.settings
    directory = hosted_dir(settings, token, created.json()["id"])
    assert (directory / "amounts.csv").read_bytes() == b"a,b\n1,2\n"


async def test_file_purpose_not_implemented(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/files",
        headers=auth("files-purpose"),
        data={"purpose": "fine-tune"},
        files={"file": ("data.jsonl", b"{}", "application/json")},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "not_implemented"
    assert error["code"] == "fine-tune"


async def test_file_id_missing_or_deleted_is_not_found(client: AsyncClient) -> None:
    token = "files-missing"
    uploaded = await client.post(
        "/v1/files",
        headers=auth(token),
        data={"purpose": "assistants"},
        files={"file": ("note.txt", b"hi", "text/plain")},
    )
    file_id = uploaded.json()["id"]
    deleted = await client.delete(f"/v1/files/{file_id}", headers=auth(token))
    agent_id = await create_agent(client, token)
    created = [
        await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={
                "agent_id": agent_id,
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {
                            "type": "file_id",
                            "file_id": missing,
                            "path": "/workspace/note.txt",
                        }
                    ],
                },
            },
        )
        for missing in (file_id, "file-missing")
    ]
    assert deleted.json()["deleted"] is True
    assert [response.status_code for response in created] == [404, 404]


async def test_file_content_disposition_non_ascii(client: AsyncClient) -> None:
    token = "files-unicode"
    name = "Bericht_Größe_✓.pdf"
    uploaded = await client.post(
        "/v1/files",
        headers=auth(token),
        data={"purpose": "user_data"},
        files={"file": (name, b"%PDF", "application/pdf")},
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["id"]
    content = await client.get(f"/v1/files/{file_id}/content", headers=auth(token))
    assert content.status_code == 200
    disposition = content.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert content.headers["x-content-type-options"] == "nosniff"


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + os.urandom(32)


def _data_url(data: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode()
    return {"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"}


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    name: str,
    data: bytes = b"a,b\n",
    content_type: str = "text/csv",
) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=headers,
        data={"purpose": "user_data"},
        files={"file": (name, data, content_type)},
    )
    assert uploaded.status_code == 200, uploaded.json()
    return str(uploaded.json()["id"])


async def _session(client: AsyncClient, headers: dict[str, str]) -> str:
    agent = await client.post(
        "/v1/agents", headers=headers, json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=headers,
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def _send(
    client: AsyncClient, headers: dict[str, str], session_id: str, *parts: Any
) -> list[str]:
    sent = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=headers,
        json=message(*parts),
    )
    assert sent.status_code == 200, sent.json()
    items = await client.get(f"/v1/agents/sessions/{session_id}/items", headers=headers)
    users = [item for item in items.json()["data"] if item["data"]["role"] == "user"]
    content = users[-1]["data"]["content"]
    if isinstance(content, str):
        return []
    return [part["file_id"] for part in content if part["type"] == "input_image"]


async def _created_order(store: Store, ids: list[str]) -> list[str]:
    async with store.session() as db:
        rows = (
            await db.execute(
                select(FileRow.id, FileRow.created_at).where(FileRow.id.in_(ids))
            )
        ).all()
    return [row.id for row in sorted(rows, key=lambda row: (row.created_at, row.id))]


def _ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.json()
    return [row["id"] for row in response.json()["data"]]


@asynccontextmanager
async def _vision_client(
    settings: Settings,
    store: Store,
    worker_secret: str,
    harness: FakeHarness | None = None,
    **kwargs: Any,
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    async with split_client_for(
        vision(settings),
        store,
        harness=harness or FakeHarness(),
        token=worker_secret,
        authenticate=Users(),
        **kwargs,
    ) as (app, client, _worker):
        yield app, client


async def test_files_list_kinds_filters_and_session_files(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        ada = auth("kinds", "ada")
        doc = await _upload(client, ada, "doc.csv")
        session_id = await _session(client, ada)
        [image] = await _send(
            client,
            ada,
            session_id,
            {"type": "input_text", "text": "see"},
            _data_url(_png()),
        )
        items = await client.get(f"/v1/agents/sessions/{session_id}/items", headers=ada)
        user_item = next(
            item["id"]
            for item in items.json()["data"]
            if item["data"]["role"] == "user"
        )
        default = await client.get("/v1/files", headers=ada)
        everything = await client.get(
            "/v1/files", headers=ada, params={"include_attachments": "true"}
        )
        listed = await client.get("/v1/apipi/files", headers=ada)
        by_kind = await client.get(
            "/v1/apipi/files", headers=ada, params={"kind": "image"}
        )
        two_kinds = await client.get(
            "/v1/apipi/files",
            headers=ada,
            params=[("kind", "file"), ("kind", "image")],
        )
        bad_kind = await client.get(
            "/v1/apipi/files", headers=ada, params={"kind": "bogus"}
        )
        by_session = await client.get(
            "/v1/apipi/files", headers=ada, params={"session_id": session_id}
        )
        by_user = await client.get(
            "/v1/apipi/files", headers=ada, params={"user_id": "ada"}
        )
        other_user = await client.get(
            "/v1/apipi/files", headers=ada, params={"user_id": "bea"}
        )
        by_purpose = await client.get(
            "/v1/apipi/files", headers=ada, params={"purpose": "assistants"}
        )
        by_name = await client.get(
            "/v1/apipi/files", headers=ada, params={"filename": "do"}
        )
        by_upper = await client.get(
            "/v1/apipi/files", headers=ada, params={"filename": "DO"}
        )
        bea_session = await client.get(
            "/v1/apipi/files",
            headers=auth("kinds", "bea"),
            params={"session_id": session_id},
        )
        foreign_session = await client.get(
            "/v1/apipi/files",
            headers=auth("kinds-other", "ada"),
            params={"session_id": session_id},
        )
        foreign_user = await client.get(
            "/v1/apipi/files",
            headers=auth("kinds-other", "ada"),
            params={"user_id": "ada"},
        )
        session_files = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=ada
        )
        foreign_files = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=auth("kinds-other")
        )
    assert _ids(default) == [doc]
    assert default.json()["object"] == "list"
    assert "kind" not in default.json()["data"][0]
    assert sorted(_ids(everything)) == sorted([doc, image])
    bodies = {row["id"]: row for row in listed.json()["data"]}
    assert bodies[image]["kind"] == "image"
    assert bodies[image]["user_id"] == "ada"
    assert bodies[image]["content_type"] == "image/png"
    assert bodies[image]["object"] == "file"
    assert bodies[doc]["kind"] == "file"
    assert bodies[doc]["user_id"] == "ada"
    assert _ids(by_kind) == [image]
    assert sorted(_ids(two_kinds)) == sorted([doc, image])
    assert bad_kind.status_code == 400
    assert _ids(by_session) == [image]
    assert sorted(_ids(by_user)) == sorted([doc, image])
    assert _ids(other_user) == []
    assert _ids(by_purpose) == []
    assert _ids(by_name) == [doc]
    assert _ids(by_upper) == []
    assert bea_session.status_code == 404
    assert foreign_session.status_code == 404
    assert _ids(foreign_user) == []
    assert session_files.status_code == 200
    assert session_files.json()["data"] == [
        {
            "file_id": image,
            "kind": "image",
            "filename": "image",
            "bytes": 40,
            "content_type": "image/png",
            "path": None,
            "item_id": user_item,
            "created_at": session_files.json()["data"][0]["created_at"],
        }
    ]
    assert session_files.json()["first_id"] == image
    assert foreign_files.status_code == 404


async def test_file_lists_paginate(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        headers = auth("pages")
        uploaded = [await _upload(client, headers, f"f{n}.csv") for n in range(5)]
        created = await _created_order(store, uploaded)
        foreign = await _upload(client, auth("pages-other"), "x.csv")
        full = _ids(await client.get("/v1/files", headers=headers))
        pages: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 2}
            if after is not None:
                params["after"] = after
            page = await client.get("/v1/files", headers=headers, params=params)
            assert page.status_code == 200
            pages.append(page.json())
            if not page.json()["has_more"]:
                break
            after = page.json()["last_id"]
        ascending = _ids(
            await client.get(
                "/v1/files", headers=headers, params={"order": "asc", "limit": 3}
            )
        )
        apipi_page = await client.get(
            "/v1/apipi/files",
            headers=headers,
            params={"limit": 2, "after": full[1], "order": "desc"},
        )
        unknown = await client.get(
            "/v1/files", headers=headers, params={"after": "file-unknown"}
        )
        foreign_after = await client.get(
            "/v1/files", headers=headers, params={"after": foreign}
        )
        apipi_foreign = await client.get(
            "/v1/apipi/files", headers=headers, params={"after": foreign}
        )
        too_small = await client.get("/v1/files", headers=headers, params={"limit": 0})
        too_big = await client.get("/v1/files", headers=headers, params={"limit": 101})
        bad_order = await client.get(
            "/v1/files", headers=headers, params={"order": "sideways"}
        )
        session_id = await _session(client, headers)
        images = await _send(
            client, headers, session_id, *[_data_url(_png()) for _ in range(3)]
        )
        path = f"/v1/apipi/sessions/{session_id}/files"
        first = await client.get(path, headers=headers, params={"limit": 2})
        second = await client.get(
            path,
            headers=headers,
            params={"limit": 2, "after": first.json()["last_id"]},
        )
        bound_asc = await client.get(path, headers=headers, params={"order": "asc"})
        session_unknown = await client.get(
            path, headers=headers, params={"after": uploaded[0]}
        )
    assert full == list(reversed(created))
    assert [len(page["data"]) for page in pages] == [2, 2, 1]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [row["id"] for page in pages for row in page["data"]] == full
    assert pages[0]["first_id"] == full[0]
    assert pages[0]["last_id"] == full[1]
    assert ascending == created[:3]
    assert _ids(apipi_page) == full[2:4]
    assert apipi_page.json()["has_more"] is True
    assert unknown.status_code == 400
    assert foreign_after.status_code == 400
    assert apipi_foreign.status_code == 400
    assert too_small.status_code == 400
    assert too_big.status_code == 400
    assert bad_order.status_code == 400
    first_ids = [row["file_id"] for row in first.json()["data"]]
    second_ids = [row["file_id"] for row in second.json()["data"]]
    assert first.json()["has_more"] is True
    assert second.json()["has_more"] is False
    assert sorted(first_ids + second_ids) == sorted(images)
    assert [row["file_id"] for row in bound_asc.json()["data"]] == list(
        reversed(first_ids + second_ids)
    )
    assert session_unknown.status_code == 400


async def test_session_delete_removes_files_only_it_uses(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (app, client):
        token = "owned"
        headers = auth(token)
        tenant_id = tenant_from_key(token)
        files = app.state.gateway.files
        kept_file = await _upload(client, headers, "photo.png", _png(), "image/png")
        first = await _session(client, headers)
        only_first, shared, by_id = await _send(
            client,
            headers,
            first,
            _data_url(_png()),
            _data_url(_png()),
            {"type": "input_image", "file_id": kept_file},
        )
        attachment = await files.create(
            tenant_id,
            data=b"notes",
            filename="notes.txt",
            purpose="user_data",
            content_type="text/plain",
            kind="attachment",
        )
        await files.bind_session(
            tenant_id,
            uuid.UUID(first),
            [attachment["id"]],
            path="attachments/notes.txt",
        )
        second = await _session(client, headers)
        await _send(client, headers, second, {"type": "input_image", "file_id": shared})
        bound = await client.get(f"/v1/apipi/sessions/{first}/files", headers=headers)
        removed = await client.delete(f"/v1/agents/sessions/{first}", headers=headers)
        after_first: dict[str, int] = {}
        for file_id in (only_first, shared, kept_file, attachment["id"]):
            got = await client.get(f"/v1/files/{file_id}", headers=headers)
            after_first[file_id] = got.status_code
        gone_bytes = await files.objects.get(
            NS_FILES, file_object_id(tenant_id, only_first)
        )
        second_files = await client.get(
            f"/v1/apipi/sessions/{second}/files", headers=headers
        )
        await client.delete(f"/v1/agents/sessions/{second}", headers=headers)
        shared_after = await client.get(f"/v1/files/{shared}", headers=headers)
        file_after = await client.get(f"/v1/files/{kept_file}", headers=headers)
    assert by_id == kept_file
    paths = {row["file_id"]: row["path"] for row in bound.json()["data"]}
    assert paths == {
        only_first: None,
        shared: None,
        kept_file: None,
        attachment["id"]: "attachments/notes.txt",
    }
    assert removed.status_code == 200
    assert after_first == {
        only_first: 404,
        shared: 200,
        kept_file: 200,
        attachment["id"]: 404,
    }
    assert gone_bytes is None
    assert [row["file_id"] for row in second_files.json()["data"]] == [shared]
    assert shared_after.status_code == 404
    assert file_after.status_code == 200


async def test_file_delete_removes_session_bindings(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        headers = auth("unbind")
        session_id = await _session(client, headers)
        [image] = await _send(client, headers, session_id, _data_url(_png()))
        deleted = await client.delete(f"/v1/files/{image}", headers=headers)
        bound = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=headers
        )
        content = await client.get(f"/v1/files/{image}/content", headers=headers)
        items = await client.get(
            f"/v1/agents/sessions/{session_id}/items", headers=headers
        )
    assert deleted.json()["deleted"] is True
    assert bound.json()["data"] == []
    assert content.status_code == 404
    first_user = next(
        item for item in items.json()["data"] if item["data"]["role"] == "user"
    )
    assert first_user["data"]["content"] == [{"type": "input_image", "file_id": image}]


async def test_unbound_attachments_are_swept_after_the_ttl(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (app, client):
        token = "sweep"
        headers = auth(token)
        tenant_id = tenant_from_key(token)
        files = app.state.gateway.files

        async def _attachment(name: str) -> str:
            created = await files.create(
                tenant_id,
                data=b"bytes",
                filename=name,
                purpose="user_data",
                kind="attachment",
            )
            return str(created["id"])

        plain = await _upload(client, headers, "plain.csv")
        loose = await _attachment("loose.txt")
        bound = await _attachment("bound.txt")
        session_id = await _session(client, headers)
        await files.bind_session(
            tenant_id, uuid.UUID(session_id), [bound], path="attachments/b"
        )
        early = await files.sweep_attachments()
        later = await files.sweep_attachments(
            now=utc_now() + settings.attachment_ttl + timedelta(minutes=1)
        )
        status: dict[str, int] = {}
        for file_id in (loose, bound, plain):
            got = await client.get(f"/v1/files/{file_id}", headers=headers)
            status[file_id] = got.status_code
        loose_bytes = await files.objects.get(
            NS_FILES, file_object_id(tenant_id, loose)
        )
    assert settings.attachment_ttl == timedelta(hours=24)
    assert early == 0
    assert later == 1
    assert status == {loose: 404, bound: 200, plain: 200}
    assert loose_bytes is None


async def test_file_lists_apply_the_authorization_filter(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    allowed: set[str] = set()
    deny_read: list[bool] = []

    async def authorize(
        identity: AuthIdentity, action: str, resource_type: str, resource_id: str | None
    ) -> AuthFilter | AuthReject | None:
        del identity, resource_id
        if action == "file.list" and resource_type == "file":
            return AuthFilter(ids=frozenset(allowed))
        if action == "session.read" and deny_read:
            return AuthReject(status_code=403, code="forbidden", message="Forbidden")
        return None

    async with _vision_client(settings, store, worker_secret, authorize=authorize) as (
        _app,
        client,
    ):
        headers = auth("authz")
        uploaded = [await _upload(client, headers, f"a{n}.csv") for n in range(3)]
        allowed.add(uploaded[1])
        session_id = await _session(client, headers)
        [image] = await _send(client, headers, session_id, _data_url(_png()))
        listed = await client.get("/v1/files", headers=headers, params={"limit": 1})
        apipi = await client.get("/v1/apipi/files", headers=headers)
        hidden_after = await client.get(
            "/v1/files", headers=headers, params={"after": uploaded[0]}
        )
        deny_read.append(True)
        denied = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=headers
        )
        denied_filter = await client.get(
            "/v1/apipi/files", headers=headers, params={"session_id": session_id}
        )
        deny_read.clear()
        hidden = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=headers
        )
        hidden_image_after = await client.get(
            f"/v1/apipi/sessions/{session_id}/files",
            headers=headers,
            params={"after": image},
        )
        allowed.add(image)
        shown = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=headers
        )
        by_session = await client.get(
            "/v1/apipi/files", headers=headers, params={"session_id": session_id}
        )
    assert _ids(listed) == [uploaded[1]]
    assert listed.json()["has_more"] is False
    assert _ids(apipi) == [uploaded[1]]
    assert hidden_after.status_code == 400
    assert denied.status_code == 403
    assert denied_filter.status_code == 403
    assert hidden.json()["data"] == []
    assert hidden_image_after.status_code == 400
    assert [row["file_id"] for row in shown.json()["data"]] == [image]
    assert _ids(by_session) == [image]


async def test_vision_upload_is_an_image(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    small = settings.model_copy(update={"max_image_bytes": 64})
    async with _vision_client(small, store, worker_secret) as (_app, client):
        headers = auth("vision")
        uploaded = await client.post(
            "/v1/files",
            headers=headers,
            data={"purpose": "vision"},
            files={"file": ("photo.png", _png(), "image/png")},
        )
        not_image = await client.post(
            "/v1/files",
            headers=headers,
            data={"purpose": "vision"},
            files={"file": ("notes.txt", b"words", "text/plain")},
        )
        too_big = await client.post(
            "/v1/files",
            headers=headers,
            data={"purpose": "vision"},
            files={"file": ("big.png", _png() + b"x" * 64, "image/png")},
        )
        image = uploaded.json()["id"]
        session_id = await _session(client, headers)
        sent = await _send(
            client, headers, session_id, {"type": "input_image", "file_id": image}
        )
        default = await client.get("/v1/files", headers=headers)
        everything = await client.get(
            "/v1/files", headers=headers, params={"include_attachments": "true"}
        )
        listed = await client.get(
            "/v1/apipi/files", headers=headers, params={"kind": "image"}
        )
        bound = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=headers
        )
    assert uploaded.status_code == 200
    assert uploaded.json()["purpose"] == "vision"
    assert not_image.status_code == 400
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "payload_too_large"
    assert sent == [image]
    assert _ids(default) == []
    assert _ids(everything) == [image]
    assert _ids(listed) == [image]
    assert listed.json()["data"][0]["purpose"] == "vision"
    assert [row["file_id"] for row in bound.json()["data"]] == [image]
    assert bound.json()["data"][0]["kind"] == "image"


async def test_attachments_used_as_environment_files_become_files(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (app, client):
        token = "promote"
        headers = auth(token)
        tenant_id = tenant_from_key(token)
        files = app.state.gateway.files
        await _upload(client, headers, "plain.csv")

        async def _attachment(name: str) -> str:
            created = await files.create(
                tenant_id,
                data=b"bytes",
                filename=name,
                purpose="user_data",
                kind="attachment",
            )
            return str(created["id"])

        in_session = await _attachment("session.txt")
        in_defaults = await _attachment("defaults.txt")
        loose = await _attachment("loose.txt")
        agent = await client.post(
            "/v1/agents",
            headers=headers,
            json={
                "name": "bot",
                "model": "test",
                "session_defaults": {
                    "environment": {
                        "type": "openai_hosted",
                        "files": [
                            {
                                "type": "file_id",
                                "file_id": in_defaults,
                                "path": "data/defaults.txt",
                            }
                        ],
                    }
                },
            },
        )
        assert agent.status_code == 200, agent.json()
        created = await client.post(
            "/v1/agents/sessions",
            headers=headers,
            json={
                "agent_id": agent.json()["id"],
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {
                            "type": "file_id",
                            "file_id": in_session,
                            "path": "data/session.txt",
                        }
                    ],
                },
            },
        )
        assert created.status_code == 200, created.json()
        swept = await files.sweep_attachments(
            now=utc_now() + settings.attachment_ttl + timedelta(minutes=1)
        )
        listed = await client.get("/v1/apipi/files", headers=headers)
    kinds = {row["id"]: row["kind"] for row in listed.json()["data"]}
    assert swept == 1
    assert loose not in kinds
    assert kinds[in_session] == "file"
    assert kinds[in_defaults] == "file"


async def test_user_files_are_visible_only_to_their_user(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (app, client):
        token = "user-files"
        tenant_id = tenant_from_key(token)
        u1, u2, anyone = auth(token, "u1"), auth(token, "u2"), auth(token)
        files = app.state.gateway.files

        async def _attachment(user_id: str | None) -> str:
            created = await files.create(
                tenant_id,
                data=b"bytes",
                filename="note.txt",
                purpose="user_data",
                kind="attachment",
                user_id=user_id,
            )
            return str(created["id"])

        agent_file = await _upload(client, u1, "agent.csv")
        attachment = await _attachment("u1")
        shared = await _attachment(None)
        uploaded = await client.post(
            "/v1/files",
            headers=u1,
            data={"purpose": "vision"},
            files={"file": ("photo.png", _png(), "image/png")},
        )
        image = str(uploaded.json()["id"])
        u2_session = await _session(client, u2)
        await files.bind_session(tenant_id, uuid.UUID(u2_session), [image, agent_file])
        everything = {agent_file, attachment, shared, image}
        mine = {agent_file, shared}

        async def _lists(headers: dict[str, str]) -> list[set[str]]:
            return [
                set(
                    _ids(
                        await client.get(
                            "/v1/files",
                            headers=headers,
                            params={"include_attachments": "true"},
                        )
                    )
                ),
                set(_ids(await client.get("/v1/apipi/files", headers=headers))),
            ]

        u1_lists = await _lists(u1)
        u2_lists = await _lists(u2)
        anyone_lists = await _lists(anyone)
        u2_by_user = await client.get(
            "/v1/apipi/files", headers=u2, params={"user_id": "u1"}
        )
        u2_images = await client.get(
            "/v1/apipi/files", headers=u2, params={"kind": "image"}
        )
        u2_after = [
            await client.get(
                path,
                headers=u2,
                params={"after": file_id, "include_attachments": "true"},
            )
            for path in ("/v1/files", "/v1/apipi/files")
            for file_id in (attachment, image)
        ]
        u1_after = await client.get(
            "/v1/apipi/files", headers=u1, params={"after": attachment}
        )
        session_path = f"/v1/apipi/sessions/{u2_session}/files"
        u2_bound = await client.get(session_path, headers=u2)
        u2_bound_after = await client.get(
            session_path, headers=u2, params={"after": image}
        )
        u2_reads = [
            await client.get(f"/v1/files/{file_id}{suffix}", headers=u2)
            for file_id in (attachment, image)
            for suffix in ("", "/content")
        ]
        u2_shared = await client.get(f"/v1/files/{shared}/content", headers=u2)
        u2_agent_file = await client.get(f"/v1/files/{agent_file}", headers=u2)
        u1_reads = [
            await client.get(f"/v1/files/{file_id}/content", headers=u1)
            for file_id in (attachment, image)
        ]
        anyone_read = await client.get(f"/v1/files/{attachment}", headers=anyone)
        u2_delete = await client.delete(f"/v1/files/{attachment}", headers=u2)
        u1_delete = await client.delete(f"/v1/files/{attachment}", headers=u1)
    assert u1_lists == [everything, everything]
    assert anyone_lists == [everything, everything]
    assert u2_lists == [mine, mine]
    assert _ids(u2_by_user) == [agent_file]
    assert _ids(u2_images) == []
    assert [response.status_code for response in u2_after] == [400] * 4
    assert u1_after.status_code == 200
    assert [row["file_id"] for row in u2_bound.json()["data"]] == [agent_file]
    assert u2_bound_after.status_code == 400
    assert [response.status_code for response in u2_reads] == [404] * 4
    assert u2_shared.status_code == 200
    assert u2_agent_file.status_code == 200
    assert [response.status_code for response in u1_reads] == [200, 200]
    assert anyone_read.status_code == 200
    assert u2_delete.status_code == 404
    assert u1_delete.status_code == 200


async def test_user_files_of_another_user_are_not_session_inputs(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (app, client):
        token = "user-inputs"
        tenant_id = tenant_from_key(token)
        u1, u2 = auth(token, "u1"), auth(token, "u2")
        agent_file = await _upload(client, u1, "agent.csv")
        created = await app.state.gateway.files.create(
            tenant_id,
            data=b"secret",
            filename="note.txt",
            purpose="user_data",
            kind="attachment",
            user_id="u1",
        )
        attachment = str(created["id"])

        def _env(file_id: str) -> dict[str, Any]:
            return {
                "type": "openai_hosted",
                "files": [
                    {"type": "file_id", "file_id": file_id, "path": "data/in.txt"}
                ],
            }

        u1_agent = await client.post(
            "/v1/agents",
            headers=u1,
            json={
                "name": "bot",
                "model": "test",
                "session_defaults": {"environment": _env(agent_file)},
            },
        )
        assert u1_agent.status_code == 200, u1_agent.json()
        agent_id = u1_agent.json()["id"]
        u2_agent_create = await client.post(
            "/v1/agents",
            headers=u2,
            json={
                "name": "bot",
                "model": "test",
                "session_defaults": {"environment": _env(attachment)},
            },
        )
        u2_agent_update = await client.post(
            f"/v1/agents/{agent_id}",
            headers=u2,
            json={"session_defaults": {"environment": _env(attachment)}},
        )
        u2_session_create = await client.post(
            "/v1/agents/sessions",
            headers=u2,
            json={"agent_id": agent_id, "environment": _env(attachment)},
        )
        u1_kinds = {
            row["id"]: row["kind"]
            for row in (await client.get("/v1/apipi/files", headers=u1)).json()["data"]
        }
        u2_from_defaults = await client.post(
            "/v1/agents/sessions", headers=u2, json={"agent_id": agent_id}
        )
        u2_agent_file = await client.post(
            "/v1/agents/sessions",
            headers=u2,
            json={"agent_id": agent_id, "environment": _env(agent_file)},
        )
        u1_session = await client.post(
            "/v1/agents/sessions",
            headers=u1,
            json={"agent_id": agent_id, "environment": _env(attachment)},
        )
    assert u2_agent_create.status_code == 404
    assert u2_agent_update.status_code == 404
    assert u2_session_create.status_code == 404
    assert u1_kinds[attachment] == "attachment"
    assert u2_from_defaults.status_code == 200, u2_from_defaults.json()
    assert u2_agent_file.status_code == 200, u2_agent_file.json()
    assert u1_session.status_code == 200, u1_session.json()


@pytest.mark.parametrize(
    ("part_type", "environment", "prompt"),
    [
        ("input_file", "none", '<file name="plan.md">\nsecret plan\n</file>'),
        ("input_file", "openai_hosted", "Attached: attachments/plan.md (md, 11 B)"),
        ("input_image", "none", ""),
    ],
)
async def test_user_files_of_another_user_are_not_input_parts(
    settings: Settings,
    store: Store,
    worker_secret: str,
    part_type: str,
    environment: str,
    prompt: str,
) -> None:
    harness = FakeHarness()
    image = part_type == "input_image"
    async with _vision_client(settings, store, worker_secret, harness) as (
        app,
        client,
    ):
        token = "user-parts"
        users = {
            "u1": auth(token, "u1"),
            "u2": auth(token, "u2"),
            "anyone": auth(token),
        }
        other = auth("user-parts-other")

        async def _create(headers: dict[str, str], *content: Any) -> Any:
            agent = await client.post(
                "/v1/agents", headers=headers, json={"name": "bot", "model": "test"}
            )
            body: dict[str, Any] = {
                "agent_id": agent.json()["id"],
                "environment": {"type": environment},
            }
            if content:
                body["input"] = {"role": "user", "content": list(content)}
            return await client.post("/v1/agents/sessions", headers=headers, json=body)

        first = await _create(users["u1"])
        assert first.status_code == 200, first.json()
        kind = "image" if image else "attachment"
        created = await app.state.gateway.files.create(
            tenant_from_key(token),
            data=_png() if image else b"secret plan",
            filename="plan.png" if image else "plan.md",
            purpose="vision" if image else "user_data",
            content_type="image/png" if image else "text/markdown",
            kind=kind,
            user_id="u1",
        )
        part = {"type": part_type, "file_id": str(created["id"])}
        refused = [await _create(headers, part) for headers in (users["u2"], other)]
        lists = [
            await client.get("/v1/agents/sessions", headers=headers)
            for headers in (users["u2"], other)
        ]
        sessions = {"u1": first.json()["id"]}
        for name in ("u2", "anyone"):
            session = await _create(users[name])
            assert session.status_code == 200, session.json()
            sessions[name] = session.json()["id"]
        sent: dict[str, int] = {}
        for name, headers in users.items():
            response = await client.post(
                f"/v1/agents/sessions/{sessions[name]}/events",
                headers=headers,
                json=message(part),
            )
            sent[name] = response.status_code
        kinds = await client.get(
            "/v1/apipi/files", headers=users["u1"], params={"kind": kind}
        )
    assert [response.status_code for response in refused] == [404, 404]
    assert [listed.json()["data"] for listed in lists] == [[], []]
    assert sent == {"u1": 200, "u2": 404, "anyone": 200}
    assert harness.prompts == [prompt, prompt]
    assert len(harness.images) == (2 if image else 0)
    assert _ids(kinds) == [created["id"]]


async def _image_agent(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    uploaded = await client.post(
        "/v1/files",
        headers=headers,
        data={"purpose": "vision"},
        files={"file": ("photo.png", _png(), "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.json()
    image = str(uploaded.json()["id"])
    agent = await client.post(
        "/v1/agents",
        headers=headers,
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": {
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {"type": "file_id", "file_id": image, "path": "data/a.png"}
                    ],
                }
            },
        },
    )
    assert agent.status_code == 200, agent.json()
    return image, str(agent.json()["id"])


async def test_images_in_agent_defaults_become_agent_files(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        u1, u2 = auth("agent-images", "u1"), auth("agent-images", "u2")
        image, agent_id = await _image_agent(client, u1)
        kinds = {
            row["id"]: row["kind"]
            for row in (await client.get("/v1/apipi/files", headers=u1)).json()["data"]
        }
        u2_read = await client.get(f"/v1/files/{image}", headers=u2)
        u2_export = await client.get(f"/v1/apipi/agents/{agent_id}/export", headers=u2)
        u2_session = await client.post(
            "/v1/agents/sessions", headers=u2, json={"agent_id": agent_id}
        )
    assert kinds[image] == "file"
    assert u2_read.status_code == 200
    assert u2_export.status_code == 200
    assert u2_session.status_code == 200, u2_session.json()


async def test_user_files_of_an_older_agent_are_not_exported_to_another_user(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        token = "agent-older"
        u1, u2 = auth(token, "u1"), auth(token, "u2")
        image, agent_id = await _image_agent(client, u1)
        async with store.session() as db:
            row = await get_file(db, tenant_from_key(token), image)
            assert row is not None
            row.kind = "image"
        export = f"/v1/apipi/agents/{agent_id}/export"
        u2_export = await client.get(export, headers=u2)
        u2_template = await client.post(
            "/v1/apipi/templates", headers=u2, json={"agent_id": agent_id}
        )
        u1_export = await client.get(export, headers=u1)
    assert u2_export.status_code == 404
    assert u2_template.status_code == 404
    assert u1_export.status_code == 200
