"""Socket-level artifact presign and shared-root checks (#448)."""

import uuid
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.repo import list_artifacts
from apipi.worker.client import answer_store_check


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
        local_store_dir=str(tmp_path / "shared"),
    )


async def _session(client: AsyncClient, token: str) -> tuple[uuid.UUID, uuid.UUID]:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "b", "model": "test"}
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent.json()["id"], "environment": {"type": "none"}},
    )
    assert created.status_code == 200
    tenant_id = _tenant(token)
    return tenant_id, uuid.UUID(created.json()["id"])


async def test_presign_reply_over_socket(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "sock-presign"
        tenant_id, session_id = await _session(client, token)
        transport = client._transport
        assert isinstance(transport, ASGITransport)
        real_app = cast(Any, transport.app)
        worker = FakeWorker(real_app, worker_secret)
        hello = await worker.connect()
        assert hello["ok"] is True
        assert hello.get("store_check") is not None
        # Prove the shared root so the socket stays usable.
        proof = answer_store_check(settings, hello)
        assert proof is not None
        await worker.send_json(proof)
        # Lease the session to this worker via the hub.
        conn = real_app.state.workers.get(uuid.UUID(worker.worker_id))
        assert conn is not None
        from datetime import timedelta

        from apipi.store.models import utc_now
        from apipi.store.repo import set_session_lease

        async with store.session() as db:
            await set_session_lease(
                db,
                tenant_id,
                session_id,
                worker_id=conn.worker_id,
                lease_id=uuid.uuid4(),
                lease_until=utc_now() + timedelta(seconds=30),
            )
        request_id = uuid.uuid4()
        await worker.send_json(
            {
                "v": 2,
                "session_id": str(session_id),
                "seq": 1,
                "type": "artifact.presign",
                "payload": {
                    "request_id": str(request_id),
                    "kind": "artifact",
                    "filename": "out.txt",
                    "content_type": "text/plain",
                    "size": 5,
                    "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e"
                    "1b161e5c1fa7425e73043362938b9824",
                },
            }
        )
        seen_ack = False
        seen_reply = False
        for _ in range(20):
            message = await worker.receive_json(timeout=5)
            if message.get("type") == "ack":
                seen_ack = True
            if message.get("type") == "artifact.presign.reply":
                seen_reply = True
                assert message["ok"] is True
                assert message["request_id"] == str(request_id)
            if seen_ack and seen_reply:
                break
        assert seen_ack and seen_reply
        await worker.close()


async def test_wrong_store_proof_closes_socket(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        transport = client._transport
        assert isinstance(transport, ASGITransport)
        real_app = cast(Any, transport.app)
        worker = FakeWorker(real_app, worker_secret)
        hello = await worker.connect()
        assert hello.get("store_check") is not None
        await worker.send_json(
            {"type": "store.proof", "marker": "nope", "nonce": "wrong"}
        )
        closed = await worker.wait_close(timeout=5)
        assert closed.get("code") == 1008


async def test_filesystem_shared_root_split_simulation(
    store: Store, tmp_path: Path
) -> None:
    """API and worker share the store root but keep separate workspaces."""
    from apipi.protocol import WorkerEnvelope
    from apipi.services.ingest import IngestBatcher, flush_batch
    from apipi.services.worker_artifacts import sha256_hex
    from apipi.store.blobs import blob_key
    from apipi.store.repo import create_session, create_tenant, set_session_lease
    from apipi.worker.artifact_upload import write_shared_object

    shared = tmp_path / "shared"
    api_settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "api-sessions"),
        local_store_dir=str(shared),
    )
    worker_settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "worker-sessions"),
        local_store_dir=str(shared),
    )
    worker_id = uuid.uuid4()
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db, tenant.id, environment={"type": "none"}, metadata={}
        )

        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now_plus(),
        )
        tenant_id, session_id, key_id = tenant.id, row.id, row.key_id
    data = b"shared-bytes"
    request_id = uuid.uuid4()
    batcher = IngestBatcher()
    batcher.add(
        WorkerEnvelope.model_validate(
            {
                "v": 2,
                "session_id": str(session_id),
                "seq": 1,
                "type": "artifact.presign",
                "payload": {
                    "request_id": str(request_id),
                    "kind": "artifact",
                    "filename": "out.txt",
                    "content_type": "text/plain",
                    "size": len(data),
                    "sha256": sha256_hex(data),
                },
            }
        ),
        128,
    )
    outcome = await flush_batch(
        store, batcher.take(), worker_id=worker_id, settings=api_settings, metrics=None
    )
    assert outcome.presign_replies[0]["ok"] is True
    upload_id = uuid.UUID(outcome.presign_replies[0]["upload_id"])
    async with store.session() as db:
        from apipi.store.repo import get_artifact_upload

        upload = await get_artifact_upload(db, tenant_id, upload_id)
        assert upload is not None
        object_id = blob_key(tenant_id, key_id, session_id, upload.artifact_id)
    # The worker writes from its own process view of the same shared root.
    relative = write_shared_object(worker_settings, object_id, data)
    batcher2 = IngestBatcher()
    batcher2.add(
        WorkerEnvelope.model_validate(
            {
                "v": 2,
                "session_id": str(session_id),
                "seq": 2,
                "type": "artifact.completed",
                "payload": {
                    "upload_id": str(upload_id),
                    "path": relative,
                    "size": len(data),
                    "sha256": sha256_hex(data),
                    "name": "out.txt",
                },
            }
        ),
        128,
    )
    outcome2 = await flush_batch(
        store, batcher2.take(), worker_id=worker_id, settings=api_settings, metrics=None
    )
    assert outcome2.rejected == []
    async with store.session() as db:
        artifacts = await list_artifacts(db, tenant_id, session_id)
        assert artifacts is not None
        assert len(artifacts) == 1


def utc_now_plus():
    from datetime import timedelta

    from apipi.store.models import utc_now

    return utc_now() + timedelta(seconds=30)
