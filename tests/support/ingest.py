import uuid
from datetime import timedelta
from typing import Any, NamedTuple

from apipi.common.metrics import Metrics
from apipi.protocol import WorkerEnvelope
from apipi.services.ingest import IngestBatcher, IngestOutcome, flush_batch
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import create_session, create_tenant, set_session_lease


class Leased(NamedTuple):
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    lease_id: uuid.UUID
    key_id: str


async def leased_session(store: Store, worker_id: uuid.UUID, **session: Any) -> Leased:
    session.setdefault("environment", {"type": "none"})
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(db, tenant.id, **session)
        lease_id = uuid.uuid4()
        await set_session_lease(
            db,
            tenant.id,
            row.id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
        return Leased(tenant.id, row.id, lease_id, row.key_id)


def frame(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    turn_id = payload.get("turn_id")
    if turn_id is None and isinstance(payload.get("data"), dict):
        maybe = payload["data"].get("turn_id")
        turn_id = maybe if isinstance(maybe, str) else None
    return {
        "v": 2,
        "session_id": str(session_id),
        "turn_id": turn_id,
        "seq": seq,
        "type": type,
        "payload": payload,
    }


def envelope(
    session_id: uuid.UUID, seq: int, type: str, payload: dict[str, Any]
) -> WorkerEnvelope:
    return WorkerEnvelope.model_validate(frame(session_id, seq, type, payload))


async def flush(
    store: Store,
    worker_id: uuid.UUID,
    envelopes: list[WorkerEnvelope],
    settings: Any = None,
    *,
    metrics: Metrics | None = None,
    objects: Any = None,
) -> IngestOutcome:
    batcher = IngestBatcher()
    for item in envelopes:
        batcher.add(item, 128)
    return await flush_batch(
        store,
        batcher.take(),
        worker_id=worker_id,
        settings=settings,
        metrics=metrics,
        objects=objects,
    )
