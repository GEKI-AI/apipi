import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import split_client_for, wait_for_event_types

from apipi.common.dirs import pi_session_file, store_root
from apipi.common.errors import ApiError
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.protocol import check_command_size, redact_context
from apipi.services.turn_context import build_turn_context
from apipi.store.blobs import blob_store
from apipi.store.engine import Store
from apipi.store.repo import get_session
from apipi.worker.fake_harness import FakeHarness
from apipi.workerhub.commands import _check_command_context

pytest_plugins = ["tests.support.mcp_http_server"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> uuid.UUID:
    return uuid5(NAMESPACE_URL, hash_token(token))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "store"),
        mcp_allow_hosts="127.0.0.1",
    )


@pytest.fixture
def worker_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        local_store_dir=settings.local_store_dir,
        worker_accepts=["none", "microvm"],
        mcp_allow_hosts="127.0.0.1",
    )


@pytest.fixture
def worker_harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
async def client(settings: Settings, store: Store) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


def _zip_skill(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\n---\nDo things.\n")
    return buffer.getvalue()


async def _agent(
    client: AsyncClient,
    token: str,
    tools: list[dict] | None = None,
    reasoning: dict | None = None,
) -> str:
    body: dict = {
        "name": "bot",
        "model": "test",
        "instructions": "Follow the plan.",
    }
    if tools is not None:
        body["tools"] = tools
    if reasoning is not None:
        body["reasoning"] = reasoning
    response = await client.post("/v1/agents", headers=_auth(token), json=body)
    assert response.status_code == 200
    return str(response.json()["id"])


async def _file(client: AsyncClient, token: str) -> str:
    uploaded = await client.post(
        "/v1/files",
        headers=_auth(token),
        data={"purpose": "user_data"},
        files={"file": ("notes.txt", b"workspace notes", "text/plain")},
    )
    assert uploaded.status_code == 200
    return str(uploaded.json()["id"])


async def _skill(client: AsyncClient, token: str) -> str:
    uploaded = await client.post(
        "/v1/skills",
        headers=_auth(token),
        files={"files": ("demo.zip", _zip_skill("demo"), "application/zip")},
    )
    assert uploaded.status_code == 200
    return str(uploaded.json()["id"])


async def _idle_session(
    client: AsyncClient,
    token: str,
    agent_id: str,
    file_id: str,
    skill_id: str,
) -> str:
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent_id,
            "environment": {
                "type": "openai_hosted",
                "files": [{"type": "file_id", "file_id": file_id, "path": "notes.txt"}],
                "skills": [{"type": "skill_reference", "skill_id": skill_id}],
            },
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


def _walk(value: object) -> None:
    assert not isinstance(value, bytes), "command context must not contain bytes"
    if isinstance(value, dict):
        for item in value.values():
            _walk(item)
    elif isinstance(value, list):
        for item in value:
            _walk(item)


async def test_turn_start_command_carries_turn_context(
    settings: Settings,
    store: Store,
    client: AsyncClient,
    worker_secret: str,
) -> None:
    token = "ctx-command"
    tenant_id = _tenant(token)
    agent_id = await _agent(
        client,
        token,
        tools=[
            {
                "type": "function",
                "name": "lookup",
                "description": "Look up a record.",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
        reasoning={"effort": "high"},
    )
    session_id = await _idle_session(
        client, token, agent_id, await _file(client, token), await _skill(client, token)
    )
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = cast(Any, transport.app)
    context = await app.state.sessions._turn_context(
        tenant_id,
        uuid.UUID(session_id),
        [],
        api_key="model-key",
        key_id=None,
        user_id=None,
        org_id=None,
    )
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect(capacity=2, accepts=["none", "microvm"])
    assert hello["ok"] is True
    try:
        command = await app.state.workers.acquire(
            store,
            tenant_id,
            uuid.UUID(session_id),
            op="turn.start",
            payload={
                "tenant_id": str(tenant_id),
                "text": "hi",
                "context": context,
            },
        )
    finally:
        await worker.close()
    assert command is not None
    assert command["op"] == "turn.start"
    payload = command["payload"]
    assert payload["context"]["agent"]["model"] == "test"
    assert payload["context"]["agent"]["instructions"] == "Follow the plan."
    assert payload["context"]["agent"]["thinking"] == "high"
    assert [tool["name"] for tool in payload["context"]["agent"]["function_tools"]] == [
        "lookup"
    ]
    assert payload["context"]["session"]["environment"]["type"] == "openai_hosted"
    assert payload["context"]["session"]["idle_ttl_seconds"] is not None
    assert payload["context"]["model"]["api_key"] == "model-key"
    assert len(payload["context"]["files"]) == 1
    assert len(payload["context"]["skills"]) == 1
    file_ref = payload["context"]["files"][0]
    assert file_ref["url"] is None
    assert isinstance(file_ref["local_path"], str)
    assert (store_root(settings) / file_ref["local_path"]).is_file()
    assert payload["context"]["pi_session"]["present"] is False
    _walk(payload)
    check_command_size(payload)


async def test_worker_runs_turn_from_context_without_db_reads(
    settings: Settings,
    worker_settings: Settings,
    store: Store,
    worker_secret: str,
    worker_harness: FakeHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "ctx-worker"
    tenant_id = _tenant(token)
    async with split_client_for(
        settings,
        store,
        harness=worker_harness,
        token=worker_secret,
        worker_settings=worker_settings,
    ) as (_app, client, _worker):
        agent_id = await _agent(
            client,
            token,
            tools=[
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Look up a record.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            reasoning={"effort": "high"},
        )
        session_id = await _idle_session(
            client,
            token,
            agent_id,
            await _file(client, token),
            await _skill(client, token),
        )
        session_uuid = uuid.UUID(session_id)
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_uuid)
            assert row is not None
            blob_id = uuid.uuid4()
            await blob_store(worker_settings).put(
                tenant_id, row.key_id, session_uuid, blob_id, b"pi-history"
            )
            row.pi_session_id = blob_id

        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "text": "hi"},
        )
        assert posted.status_code == 200
        await wait_for_event_types(
            client, token, session_id, "agent.session.turn.completed"
        )
    assert worker_harness.instructions is not None
    assert "Follow the plan." in worker_harness.instructions
    assert worker_harness.function_tools is not None
    assert [tool["name"] for tool in worker_harness.function_tools] == ["lookup"]
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_uuid)
        assert row is not None
        directory = row.environment.get("directory")
    assert isinstance(directory, str) and directory
    assert (Path(directory) / "notes.txt").read_text() == "workspace notes"
    assert (Path(directory) / ".agents" / "skills" / "demo" / "SKILL.md").is_file()
    assert pi_session_file(Path(directory)).read_bytes() == b"pi-history"


async def test_followup_turn_rebuilds_mcp_from_db_and_vault(
    settings: Settings,
    store: Store,
    client: AsyncClient,
    mcp_server: tuple[str, dict[str, str]],
) -> None:
    mcp_url, _seen = mcp_server
    token = "ctx-mcp"
    tenant_id = _tenant(token)
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
            "tools": [{"type": "mcp", "server_label": "mock", "server_url": mcp_url}],
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
    session_uuid = uuid.UUID(created.json()["id"])
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = cast(Any, transport.app)
    service = app.state.sessions
    assert not hasattr(service, "mcp_http")
    first = await service._turn_context(
        tenant_id,
        session_uuid,
        await service._mcp_servers(tenant_id, session_uuid),
        api_key=None,
        key_id=None,
        user_id=None,
        org_id=None,
    )
    assert first["mcp"][0]["headers"] == {"Authorization": "Bearer vault-secret"}
    rotated = await client.post(
        f"/v1/agents/vaults/{vault.json()['id']}/credentials/{cred.json()['id']}",
        headers=_auth(token),
        json={"auth": {"type": "static_bearer", "token": "rotated-secret"}},
    )
    assert rotated.status_code == 200
    second = await build_turn_context(
        store,
        settings,
        tenant_id,
        session_uuid,
        mcp_servers=await service._mcp_servers(tenant_id, session_uuid),
    )
    assert second["mcp"][0]["headers"] == {"Authorization": "Bearer rotated-secret"}


async def test_filesystem_refs_become_presigned_urls_on_s3(
    settings: Settings, store: Store
) -> None:
    from tests.unit.test_blobs import FakeS3

    from apipi.store.blobs import S3Store

    s3_settings = Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        local_store_dir=settings.local_store_dir,
        artifact_store="s3",
        s3_bucket="bucket",
        s3_prefix="apipi/artifacts",
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
    )
    app = create_app(
        s3_settings,
        store=store,
        objects=S3Store(s3_settings, client=FakeS3()),
    )
    token = "ctx-s3"
    tenant_id = _tenant(token)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent_id = await _agent(client, token)
        session_id = await _idle_session(
            client,
            token,
            agent_id,
            await _file(client, token),
            await _skill(client, token),
        )
    objects = S3Store(s3_settings, client=FakeS3())
    context = await build_turn_context(
        store,
        s3_settings,
        tenant_id,
        uuid.UUID(session_id),
        objects=objects,
    )
    assert context["files"][0]["url"].startswith("https://bucket.example/")
    assert context["files"][0]["local_path"] is None
    assert context["skills"][0]["url"].startswith("https://bucket.example/")
    _walk(context)


async def test_command_context_is_validated_and_redacted() -> None:
    raw: dict = {
        "session": {
            "environment": {"type": "none"},
            "metadata": {},
            "required_actions": [],
            "key_id": "key",
        },
        "model": {"api_key": "live-secret"},
        "mcp": [
            {
                "server_label": "mock",
                "server_url": "https://mcp.example/x?sig=abc",
                "headers": {"Authorization": "Bearer live-secret"},
                "allowed_tools": [],
            }
        ],
    }
    with pytest.raises(ApiError) as too_big:
        _check_command_context(
            "turn.start",
            {"context": {**raw, "agent": {"instructions": "x" * 300_000}}},
        )
    assert too_big.value.code == "payload_too_large"
    with pytest.raises(ApiError) as bad:
        _check_command_context(
            "turn.start", {"context": {**raw, "files": [{"path": "x", "data": b"y"}]}}
        )
    assert bad.value.code == "invalid_request"
    redacted = redact_context(raw)
    assert "live-secret" not in str(redacted)
    assert "sig=abc" not in str(redacted)
