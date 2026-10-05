import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from apipi.config import Settings
from apipi.protocol import parse_envelope
from apipi.services.ingest import IngestBatcher, flush_batch
from apipi.store.engine import Store
from apipi.worker.artifact_upload import handle_presign_reply
from apipi.worker.outbox import Outbox
from tests.support.config import DATABASE_URL


def s3_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        run_mode="none",
        sessions_dir=str(tmp_path / "api-sessions"),
        local_store_dir=str(tmp_path / "api-shared"),
        artifact_store="s3",
        s3_bucket="test-bucket",
        s3_prefix="apipi/artifacts",
    )


def _queued(envelopes: list[dict[str, Any]]) -> list[Any]:
    batcher = IngestBatcher()
    for envelope in envelopes:
        raw = len(json.dumps(envelope, separators=(",", ":")).encode())
        batcher.add(parse_envelope(envelope), raw)
    return batcher.take()


async def flush_outbox(
    store: Store,
    outbox: Outbox,
    session_id: uuid.UUID,
    worker_id: uuid.UUID,
    settings: Settings,
    objects: Any | None = None,
) -> Any:
    pending = outbox.pending(session_id)
    assert pending, "outbox has nothing to flush"
    outcome = await flush_batch(
        store,
        _queued(pending),
        worker_id=worker_id,
        settings=settings,
        metrics=None,
        objects=objects,
    )
    for sid, last_seq in outcome.acks.items():
        outbox.acked(sid, last_seq)
    return outcome


async def pump_until_done(
    store: Store,
    outbox: Any,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    worker_id: uuid.UUID,
    session_id: uuid.UUID,
    api_settings: Settings,
    task: asyncio.Task[Any],
    api_objects: Any,
) -> None:
    """Flush worker outbox envelopes through API ingest until `task` ends."""
    for _ in range(2000):
        if task.done():
            break
        if outbox.pending(session_id):
            outcome = await flush_outbox(
                store, outbox, session_id, worker_id, api_settings, objects=api_objects
            )
            assert outcome.rejected == [], outcome.rejected
            for reply in outcome.presign_replies:
                assert reply["ok"] is True, reply
                assert handle_presign_reply(waiters, reply) is True
        await asyncio.sleep(0.005)
    await asyncio.wait_for(asyncio.shield(task), timeout=30)
    for _ in range(10):
        if not outbox.pending(session_id):
            break
        outcome = await flush_outbox(
            store, outbox, session_id, worker_id, api_settings, objects=api_objects
        )
        assert outcome.rejected == [], outcome.rejected
        for reply in outcome.presign_replies:
            assert reply["ok"] is True, reply
            assert handle_presign_reply(waiters, reply) is True
    assert not outbox.pending(session_id)
