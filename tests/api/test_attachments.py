import asyncio
import base64
import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for, split_client_for
from tests.support.workspace import hosted_dir

from apipi.common.errors import ApiError
from apipi.config import ConfigError, Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthRequest, tenant_from_key
from apipi.gateway.content import InputFile
from apipi.gateway.tokens import hash_token
from apipi.protocol import dumps_wire
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.pi.artifacts import reap_workspaces
from apipi.workerhub.forward import forward_body

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SHEET = b"PK\x03\x04" + b"x" * 240


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": list(parts)}],
            }
        ]
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def _file(file_id: str, **extra: Any) -> dict[str, Any]:
    return {"type": "input_file", "file_id": file_id, **extra}


async def _agent(client: AsyncClient, token: str, headers: Any = None) -> str:
    agent = await client.post(
        "/v1/agents",
        headers=headers or _auth(token),
        json={"name": "bot", "model": "test"},
    )
    assert agent.status_code == 200
    return str(agent.json()["id"])


async def _session(
    client: AsyncClient,
    token: str,
    *,
    agent_id: str | None = None,
    environment: dict[str, Any] | None = None,
    headers: Any = None,
) -> str:
    created = await client.post(
        "/v1/agents/sessions",
        headers=headers or _auth(token),
        json={
            "agent_id": agent_id or await _agent(client, token, headers),
            "environment": environment or {"type": "openai_hosted"},
        },
    )
    assert created.status_code == 200, created.json()
    return str(created.json()["id"])


async def _upload(
    client: AsyncClient,
    token: str,
    data: bytes,
    filename: str,
    content_type: str = _XLSX,
) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": (filename, data, content_type)},
    )
    assert uploaded.status_code == 200, uploaded.json()
    return str(uploaded.json()["id"])


async def _send(
    client: AsyncClient, token: str, session_id: str, *parts: dict[str, Any]
) -> Any:
    return await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json=_message(*parts),
    )


async def _user_items(client: AsyncClient, token: str, session_id: str) -> list[Any]:
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=_auth(token)
    )
    assert items.status_code == 200
    return [item for item in items.json()["data"] if item["data"]["role"] == "user"]


async def _bound(client: AsyncClient, token: str, session_id: str) -> list[Any]:
    listed = await client.get(
        f"/v1/apipi/sessions/{session_id}/files?order=asc", headers=_auth(token)
    )
    assert listed.status_code == 200
    return list(listed.json()["data"])


def _spy_commands(app: Any) -> list[dict[str, Any]]:
    hub = app.state.workers
    real = hub._send
    sent: list[dict[str, Any]] = []

    async def _send_wire(conn: Any, wire: dict[str, Any]) -> None:
        if wire.get("type") == "command":
            sent.append(wire)
        await real(conn, wire)

    hub._send = _send_wire
    return sent


async def test_input_file_lands_in_attachments_before_the_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        commands = _spy_commands(app)
        token = "attach-basic"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, _SHEET, "report.xlsx")
        sent = await _send(
            client, token, session_id, _text("sum column C by month"), _file(file_id)
        )
        assert sent.status_code == 200, sent.json()
        item = (await _user_items(client, token, session_id))[-1]
        bound = await _bound(client, token, session_id)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth(token)
        )
    directory = hosted_dir(settings, token, session_id)
    assert (directory / "attachments" / "report.xlsx").read_bytes() == _SHEET
    assert harness.workspaces[-1]["attachments/report.xlsx"] == _SHEET
    assert harness.prompts == [
        "sum column C by month\nAttached: attachments/report.xlsx (xlsx, 244 B)"
    ]
    assert item["data"]["content"] == [
        {"type": "input_text", "text": "sum column C by month"},
        {
            "type": "input_file",
            "file_id": file_id,
            "filename": "report.xlsx",
            "path": "attachments/report.xlsx",
        },
    ]
    assert [(entry["file_id"], entry["path"]) for entry in bound] == [
        (file_id, "attachments/report.xlsx")
    ]
    assert bound[0]["item_id"] == item["id"]
    assert bound[0]["kind"] == "file"
    assert "files" not in session.json()["environment"]
    start = next(wire for wire in commands if wire["op"] == "turn.start")
    assert start["payload"]["parts"][1] == {
        "type": "file",
        "file_id": file_id,
        "filename": "report.xlsx",
        "mime_type": _XLSX,
        "size_bytes": len(_SHEET),
        "model_input": "workspace",
        "path": "attachments/report.xlsx",
    }
    refs = start["payload"]["context"]["session_files"]
    assert [ref["path"] for ref in refs] == ["attachments/report.xlsx"]
    assert refs[0]["size_bytes"] == len(_SHEET)
    assert refs[0]["local_path"]
    assert "xxxx" not in dumps_wire(start)
    forwarded = forward_body("turn.start", start["payload"])
    assert "context" not in forwarded["payload"]
    assert forwarded["payload"]["parts"][1] == {
        "type": "file",
        "file_id": file_id,
        "filename": "report.xlsx",
        "path": "attachments/report.xlsx",
    }


async def test_name_clash_gets_a_free_name_and_a_file_keeps_its_path(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "attach-clash"
        session_id = await _session(client, token)
        first = await _upload(client, token, b"one", "report.xlsx")
        second = await _upload(client, token, b"two", "report.xlsx")
        third = await _upload(client, token, b"three", "data.bin", "")
        assert (await _send(client, token, session_id, _file(first))).status_code == 200
        both = await _send(
            client,
            token,
            session_id,
            _file(second),
            _file(first),
            _file(third, filename="../../etc/report.xlsx"),
        )
        assert both.status_code == 200, both.json()
        items = await _user_items(client, token, session_id)
    directory = hosted_dir(settings, token, session_id)
    assert (directory / "attachments" / "report.xlsx").read_bytes() == b"one"
    assert (directory / "attachments" / "report (2).xlsx").read_bytes() == b"two"
    assert (directory / "attachments" / "report (3).xlsx").read_bytes() == b"three"
    assert harness.prompts == [
        "Attached: attachments/report.xlsx (xlsx, 3 B)",
        "Attached: attachments/report (2).xlsx (xlsx, 3 B)\n"
        "Attached: attachments/report.xlsx (xlsx, 3 B)\n"
        "Attached: attachments/report (3).xlsx (xlsx, 5 B)",
    ]
    assert [part["path"] for part in items[-1]["data"]["content"]] == [
        "attachments/report (2).xlsx",
        "attachments/report.xlsx",
        "attachments/report (3).xlsx",
    ]
    assert items[-1]["data"]["content"][2]["filename"] == "../../etc/report.xlsx"


async def test_session_create_with_only_a_file_starts_a_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "attach-create"
        file_id = await _upload(client, token, b"%PDF-1.7", "doc.pdf", "")
        inline = base64.b64encode(b"rows").decode()
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": await _agent(client, token),
                "environment": {
                    "type": "openai_hosted",
                    "files": [
                        {
                            "type": "inline",
                            "path": "attachments/doc.pdf",
                            "data": inline,
                        }
                    ],
                },
                "input": {"role": "user", "content": [_file(file_id)]},
            },
        )
        assert created.status_code == 200, created.json()
        session_id = created.json()["id"]
        item = (await _user_items(client, token, session_id))[-1]
    directory = hosted_dir(settings, token, session_id)
    assert (directory / "attachments" / "doc.pdf").read_bytes() == b"rows"
    assert (directory / "attachments" / "doc (2).pdf").read_bytes() == b"%PDF-1.7"
    assert harness.prompts == ["Attached: attachments/doc (2).pdf (pdf, 8 B)"]
    assert item["data"]["content"][0]["path"] == "attachments/doc (2).pdf"


async def test_session_files_come_back_after_a_wipe_and_stay_in_their_session(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, worker):
        commands = _spy_commands(app)
        token = "attach-wipe"
        agent_id = await _agent(client, token)
        session_id = await _session(client, token, agent_id=agent_id)
        other_id = await _session(client, token, agent_id=agent_id)
        kept = await _upload(client, token, b"keep", "keep.csv", "text/csv")
        gone = await _upload(client, token, b"gone", "gone.csv", "text/csv")
        sent = await _send(client, token, session_id, _file(kept), _file(gone))
        assert sent.status_code == 200, sent.json()
        directory = hosted_dir(settings, token, session_id)
        (directory / "attachments" / "keep.csv").write_bytes(b"edited")
        assert (
            await _send(client, token, session_id, _text("again"))
        ).status_code == 200
        edited = (directory / "attachments" / "keep.csv").read_bytes()
        deleted = await client.delete(f"/v1/files/{gone}", headers=_auth(token))
        assert deleted.status_code == 200
        await reap_workspaces(
            worker.execution.settings,
            worker.execution.pool,
            ttl_overrides=worker.execution._context_ttl,
            now=utc_now() + timedelta(hours=2),
        )
        assert not directory.exists()
        after = await _send(client, token, session_id, _text("after the wipe"))
        assert after.status_code == 200, after.json()
        other = await _send(client, token, other_id, _text("hello"))
        assert other.status_code == 200, other.json()
    assert edited == b"edited"
    assert (directory / "attachments" / "keep.csv").read_bytes() == b"keep"
    assert not (directory / "attachments" / "gone.csv").exists()
    assert not (hosted_dir(settings, token, other_id) / "attachments").exists()
    assert harness.workspaces[-2]["attachments/keep.csv"] == b"keep"
    assert "attachments/keep.csv" not in harness.workspaces[-1]
    starts = [wire for wire in commands if wire["op"] == "turn.start"]
    paths = [
        sorted(ref["path"] for ref in wire["payload"]["context"]["session_files"])
        for wire in starts
    ]
    assert paths == [
        ["attachments/gone.csv", "attachments/keep.csv"],
        ["attachments/gone.csv", "attachments/keep.csv"],
        ["attachments/keep.csv"],
        [],
    ]


async def test_attachment_limits_fail_before_the_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings.model_copy(update={"max_workspace_bytes": 100}),
        store,
        harness=harness,
        token=worker_secret,
    ) as (app, client, _worker):
        token = "attach-limits"
        inline = base64.b64encode(b"i" * 60).decode()
        session_id = await _session(
            client,
            token,
            environment={
                "type": "openai_hosted",
                "files": [{"type": "inline", "path": "inputs/a.txt", "data": inline}],
            },
        )
        small = await _upload(client, token, b"s" * 30, "small.txt", "text/plain")
        more = await _upload(client, token, b"m" * 20, "more.txt", "text/plain")
        big = await _upload(client, token, b"b" * 50, "big.txt", "text/plain")
        ok = await _send(client, token, session_id, _file(small))
        again = await _send(client, token, session_id, _file(small))
        over = await _send(client, token, session_id, _file(more))
        files = app.state.gateway.files
        files.settings = files.settings.model_copy(update={"max_file_bytes": 40})
        too_big = await _send(client, token, session_id, _file(big))
        missing = await _send(client, token, session_id, _file("file-missing"))
        no_id = await _send(client, token, session_id, {"type": "input_file"})
        bound = await _bound(client, token, session_id)
    assert ok.status_code == 200, ok.json()
    assert again.status_code == 200, again.json()
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "payload_too_large"
    assert "APIPI_MAX_WORKSPACE_BYTES" in over.json()["error"]["message"]
    assert too_big.status_code == 413
    assert "APIPI_MAX_FILE_BYTES" in too_big.json()["error"]["message"]
    assert missing.status_code == 404
    assert no_id.status_code == 400
    assert [entry["path"] for entry in bound] == ["attachments/small.txt"]
    assert len(harness.prompts) == 2


async def test_a_turn_that_does_not_start_leaves_no_binding(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (app, client, _worker):
        token = "attach-unsent"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"x", "x.txt", "text/plain")
        execution = app.state.gateway.sessions.execution

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise ApiError("invalid_request", "busy", code="capacity", status_code=429)

        real = execution.run_turn
        execution.run_turn = refuse
        failed = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(_file(file_id)),
        )
        execution.run_turn = real
        bound = await _bound(client, token, session_id)
    assert failed.status_code == 429
    assert bound == []


async def test_concurrent_messages_get_different_paths(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    async with split_client_for(
        settings, store, harness=FakeHarness(), token=worker_secret
    ) as (app, client, _worker):
        token = "attach-race"
        session_id = uuid.UUID(await _session(client, token))
        ids = [await _upload(client, token, b"r", "r.txt", "text/plain") for _ in "ab"]
        files = app.state.gateway.files
        tenant_id = tenant_from_key(token)
        results = await asyncio.gather(
            *(
                files.attach(
                    tenant_id,
                    session_id,
                    [InputFile(file_id, "r.txt", "text/plain", 1, "workspace")],
                    environment={"type": "openai_hosted"},
                )
                for file_id in ids
            )
        )
        gone = await _upload(client, token, b"g", "g.txt", "text/plain")
        assert (
            await client.delete(f"/v1/files/{gone}", headers=_auth(token))
        ).is_success
        with pytest.raises(ApiError) as raised:
            await files.attach(
                tenant_id,
                session_id,
                [
                    InputFile(ids[0], "again.txt", "text/plain", 1, "workspace"),
                    InputFile(gone, "g.txt", "text/plain", 1, "workspace"),
                ],
                environment={"type": "openai_hosted"},
            )
        bound = await _bound(client, token, str(session_id))
    paths = sorted(attached[0].path for attached, _bound in results)
    assert paths == ["attachments/r (2).txt", "attachments/r.txt"]
    assert raised.value.status_code == 404
    assert sorted(entry["path"] for entry in bound) == paths


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
        token = "attach-users"
        u1 = {**_auth(token), "X-End-User": "u1"}
        u2 = {**_auth(token), "X-End-User": "u2"}
        agent_id = await _agent(client, token, u1)
        created = await app.state.gateway.files.create(
            tenant_from_key(token),
            data=b"secret",
            filename="plan.docx",
            purpose="user_data",
            kind="attachment",
            user_id="u1",
        )
        part = _file(str(created["id"]))
        sessions = {
            name: await _session(client, token, agent_id=agent_id, headers=headers)
            for name, headers in (("u1", u1), ("u2", u2))
        }
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
    assert u2_sent.status_code == 404
    assert u1_sent.status_code == 200, u1_sent.json()
    assert harness.prompts == ["Attached: attachments/plan.docx (docx, 6 B)"]


async def test_worker_without_session_files_gets_no_attachment_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(settings), store=store)
    worker = FakeWorker(app, worker_secret)
    await worker.connect(
        accepts=["none", "microvm"],
        features=[
            "file_refs",
            "image_refs",
            "lease_cursor",
            "presign",
            "search",
            "session_stopped",
        ],
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "attach-old-worker"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"a", "a.xlsx")
        sent = await _send(client, token, session_id, _file(file_id))
    await worker.close()
    assert sent.status_code == 501
    assert sent.json()["error"]["code"] == "unsupported_op"
    assert "session_files" in sent.json()["error"]["message"]


async def test_new_attachments_reach_a_running_guest(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    pushed: list[list[tuple[str, bytes]]] = []

    class _Guest:
        async def push_files(self, files: list[tuple[str, bytes]]) -> None:
            pushed.append(files)

    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, worker):
        token = "attach-guest"
        session_id = await _session(client, token)
        first = await _upload(client, token, b"one", "one.csv", "text/csv")
        second = await _upload(client, token, b"two", "two.csv", "text/csv")
        assert (await _send(client, token, session_id, _file(first))).status_code == 200

        async def settled(_session_id: Any) -> Any:
            return _Guest()

        worker.execution.pool.settled = settled
        assert (
            await _send(client, token, session_id, _file(second))
        ).status_code == 200
        assert (
            await _send(client, token, session_id, _text("next"))
        ).status_code == 200
    assert pushed == [[("attachments/two.csv", b"two")]]


async def test_a_new_attachment_replaces_an_old_file_at_its_path(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        token = "attach-replace"
        session_id = await _session(client, token)
        directory = hosted_dir(settings, token, session_id)
        old = await _upload(client, token, b"old", "report.csv", "text/csv")
        assert (await _send(client, token, session_id, _file(old))).status_code == 200
        deleted = await client.delete(f"/v1/files/{old}", headers=_auth(token))
        assert deleted.status_code == 200
        new = await _upload(client, token, b"new", "report.csv", "text/csv")
        assert (await _send(client, token, session_id, _file(new))).status_code == 200
        replaced = (directory / "attachments" / "report.csv").read_bytes()
        (directory / "attachments" / "data.csv").write_bytes(b"agent content")
        user = await _upload(client, token, b"user content", "data.csv", "text/csv")
        assert (await _send(client, token, session_id, _file(user))).status_code == 200
        seen = harness.workspaces[-1]["attachments/data.csv"]
        (directory / "attachments" / "data.csv").write_bytes(b"agent edit")
        assert (await _send(client, token, session_id, _text("go"))).status_code == 200
        kept = (directory / "attachments" / "data.csv").read_bytes()
        again = await _send(client, token, session_id, _file(user))
        assert again.status_code == 200
        reset = (directory / "attachments" / "data.csv").read_bytes()
    assert replaced == b"new"
    assert harness.workspaces[1]["attachments/report.csv"] == b"new"
    assert seen == b"user content"
    assert kept == b"agent edit"
    assert reset == b"user content"
    assert harness.prompts[1] == "Attached: attachments/report.csv (csv, 3 B)"
    assert harness.prompts[2] == "Attached: attachments/data.csv (csv, 12 B)"


async def test_a_failed_push_ends_the_turn_and_the_retry_works(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    killed: list[str] = []

    class _Guest:
        async def push_files(self, files: list[tuple[str, bytes]]) -> None:
            raise ConfigError("microvm guest did not take the files")

    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, worker):
        token = "attach-push-fail"
        session_id = await _session(client, token)
        file_id = await _upload(client, token, b"rows", "rows.csv", "text/csv")
        pool = worker.execution.pool
        real_kill = pool.kill

        async def settled(_session_id: Any) -> Any:
            return _Guest()

        async def kill(
            session: Any, *, reason: str = "session", hook: bool = True
        ) -> None:
            killed.append(f"{reason}:{hook}")
            await real_kill(session, reason=reason, hook=hook)

        pool.settled = settled
        pool.kill = kill
        failed = await asyncio.wait_for(
            _send(client, token, session_id, _file(file_id)), timeout=15
        )
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        del pool.settled
        retry = await asyncio.wait_for(
            _send(client, token, session_id, _file(file_id)), timeout=15
        )
    assert failed.status_code == 200, failed.json()
    assert failed.json()["status"] == "failed"
    data = events.json()["data"]
    env_failed = next(
        event for event in data if event["type"] == "agent.session.environment.failed"
    )
    assert env_failed["data"]["code"] == "attachment_push_failed"
    error = next(event for event in data if event["type"] == "agent.session.error")
    assert error["data"]["retryable"] is True
    assert "agent.session.turn.created" not in [event["type"] for event in data]
    assert killed == ["push_failed:False"]
    assert retry.status_code == 200, retry.json()
    assert retry.json()["status"] == "idle"
    assert harness.prompts == ["Attached: attachments/rows.csv (csv, 4 B)"]


async def test_an_over_limit_attachment_does_not_cancel_a_running_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.hold = True
    async with split_client_for(
        settings.model_copy(update={"max_workspace_bytes": 10}),
        store,
        harness=harness,
        token=worker_secret,
    ) as (app, client, _worker):
        token = "attach-running"
        session_id = await _session(client, token)
        big = await _upload(client, token, b"b" * 20, "big.txt", "text/plain")
        queue = app.state.event_hub.subscribe(uuid.UUID(session_id))
        running = asyncio.create_task(_send(client, token, session_id, _text("go")))
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=5)
            if event["type"] == "agent.session.turn.in_progress":
                break
        over = await _send(client, token, session_id, _file(big))
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        cancelled = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.cancel"},
        )
        await asyncio.wait_for(running, timeout=10)
    assert over.status_code == 413
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.cancelled" not in types
    assert "agent.session.turn.failed" not in types
    assert cancelled.status_code == 200
    assert harness.prompts == ["go"]


async def test_an_image_and_a_file_with_one_id_leave_no_binding_when_unsent(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    vision = settings.model_copy(
        update={"model_registry": {"test": {"input": ["text", "image"]}}}
    )
    async with split_client_for(
        vision, store, harness=FakeHarness(), token=worker_secret
    ) as (app, client, _worker):
        token = "attach-image-file"
        session_id = await _session(client, token)
        png = await _upload(client, token, b"\x89PNG\r\n\x1a\n", "a.png", "image/png")
        execution = app.state.gateway.sessions.execution

        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            raise ApiError("invalid_request", "busy", code="capacity", status_code=429)

        real = execution.run_turn
        execution.run_turn = refuse
        failed = await _send(
            client,
            token,
            session_id,
            {"type": "input_image", "file_id": png},
            _file(png),
        )
        execution.run_turn = real
        bound = await _bound(client, token, session_id)
    assert failed.status_code == 429
    assert bound == []
