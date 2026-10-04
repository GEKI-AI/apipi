import base64
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from tests.support.split_worker import split_client_for

from apipi.common.objects import NS_FILES
from apipi.config import Settings
from apipi.gateway.auth import (
    AuthFilter,
    AuthIdentity,
    AuthReject,
    AuthRequest,
    tenant_from_key,
)
from apipi.gateway.tokens import hash_token
from apipi.store.blobs import file_object_id
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.worker.fake_harness import FakeHarness


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


async def test_files_crud_and_session_attach(client: AsyncClient) -> None:
    token = "files-crud"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
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
    listed = await client.get("/v1/files", headers=_auth(token))
    assert listed.json()["object"] == "list"
    assert listed.json()["data"][0]["id"] == file_id
    got = await client.get(f"/v1/files/{file_id}", headers=_auth(token))
    assert got.json()["id"] == file_id
    content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    assert content.status_code == 200
    assert content.content == b"a,b\n1,2\n"
    other = await client.get(f"/v1/files/{file_id}", headers=_auth("other-tenant"))
    assert other.status_code == 404
    agent_id = await _agent(client, token)
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
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


async def test_file_id_missing_is_not_found(client: AsyncClient) -> None:
    token = "files-missing"
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "file_id",
                        "file_id": "file-missing",
                        "path": "/workspace/x.txt",
                    }
                ],
            },
        },
    )
    assert response.status_code == 404


async def test_file_purpose_not_implemented(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/files",
        headers=_auth("files-purpose"),
        data={"purpose": "fine-tune"},
        files={"file": ("data.jsonl", b"{}", "application/json")},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "not_implemented"
    assert error["code"] == "fine-tune"


async def test_delete_file_then_attach_is_not_found(client: AsyncClient) -> None:
    token = "files-delete"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "assistants"},
        files={"file": ("note.txt", b"hi", "text/plain")},
    )
    file_id = uploaded.json()["id"]
    deleted = await client.delete(f"/v1/files/{file_id}", headers=_auth(token))
    assert deleted.json()["deleted"] is True
    agent_id = await _agent(client, token)
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {
                        "type": "file_id",
                        "file_id": file_id,
                        "path": "/workspace/note.txt",
                    }
                ],
            },
        },
    )
    assert response.status_code == 404


async def test_file_content_disposition_non_ascii(client: AsyncClient) -> None:
    token = "files-unicode"
    name = "Bericht_Größe_✓.pdf"
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": (name, b"%PDF", "application/pdf")},
    )
    assert uploaded.status_code == 200
    file_id = uploaded.json()["id"]
    content = await client.get(f"/v1/files/{file_id}/content", headers=_auth(token))
    assert content.status_code == 200
    disposition = content.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert content.headers["x-content-type-options"] == "nosniff"


_REGISTRY = {"test": {"input": ["text", "image"], "reasoning": True}}


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


def _as(token: str, user: str | None = None) -> dict[str, str]:
    headers = _auth(token)
    if user is not None:
        headers["X-End-User"] = user
    return headers


def _vision(settings: Settings) -> Settings:
    return settings.model_copy(update={"model_registry": _REGISTRY})


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
        json={
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": list(parts)}],
                }
            ]
        },
    )
    assert sent.status_code == 200, sent.json()
    items = await client.get(f"/v1/agents/sessions/{session_id}/items", headers=headers)
    users = [item for item in items.json()["data"] if item["data"]["role"] == "user"]
    content = users[-1]["data"]["content"]
    if isinstance(content, str):
        return []
    return [part["file_id"] for part in content if part["type"] == "input_image"]


def _ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.json()
    return [row["id"] for row in response.json()["data"]]


@asynccontextmanager
async def _vision_client(
    settings: Settings, store: Store, worker_secret: str, **kwargs: Any
) -> AsyncIterator[tuple[Any, AsyncClient]]:
    async with split_client_for(
        _vision(settings),
        store,
        harness=FakeHarness(),
        token=worker_secret,
        authenticate=_Users(),
        **kwargs,
    ) as (app, client, _worker):
        yield app, client


async def test_files_list_kinds_filters_and_session_files(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with _vision_client(settings, store, worker_secret) as (_app, client):
        ada = _as("kinds", "ada")
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
            headers=_as("kinds", "bea"),
            params={"session_id": session_id},
        )
        foreign_session = await client.get(
            "/v1/apipi/files",
            headers=_as("kinds-other", "ada"),
            params={"session_id": session_id},
        )
        foreign_user = await client.get(
            "/v1/apipi/files",
            headers=_as("kinds-other", "ada"),
            params={"user_id": "ada"},
        )
        session_files = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=ada
        )
        foreign_files = await client.get(
            f"/v1/apipi/sessions/{session_id}/files", headers=_as("kinds-other")
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
        headers = _as("pages")
        uploaded = [await _upload(client, headers, f"f{n}.csv") for n in range(5)]
        foreign = await _upload(client, _as("pages-other"), "x.csv")
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
    assert full == list(reversed(uploaded))
    assert [len(page["data"]) for page in pages] == [2, 2, 1]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert [row["id"] for page in pages for row in page["data"]] == full
    assert pages[0]["first_id"] == full[0]
    assert pages[0]["last_id"] == full[1]
    assert ascending == uploaded[:3]
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
        headers = _as(token)
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
        headers = _as("unbind")
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
        headers = _as(token)
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
        headers = _as("authz")
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
        headers = _as("vision")
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
        headers = _as(token)
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
