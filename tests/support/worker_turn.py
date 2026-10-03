"""Run a worker-side turn into an Outbox and ingest it into the test database."""

import uuid
from datetime import timedelta
from typing import Any, cast

from apipi.config import Settings
from apipi.protocol import WorkerEnvelope
from apipi.services.ingest import IngestBatcher, IngestOutcome, flush_batch
from apipi.services.runtime import EventHub, Harness, run_turn
from apipi.services.sink import OutboxSink
from apipi.services.turn_context import build_turn_context
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    create_session,
    create_tenant,
    get_session,
    set_session_lease,
)
from apipi.worker.outbox import Outbox


async def new_session(
    store: Store,
    *,
    model: str | None = "m1",
    status: str = "idle",
    environment: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with store.session() as db:
        tenant = await create_tenant(db, name="t")
        row = await create_session(
            db,
            tenant.id,
            model=model,
            status=status,
            environment=environment if environment is not None else {"type": "none"},
        )
        return tenant.id, row.id


async def lease_session(
    store: Store, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> uuid.UUID:
    worker_id = uuid.uuid4()
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is not None and row.lease_id is not None and row.worker_id is not None:
            return row.worker_id
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=uuid.uuid4(),
            lease_until=utc_now() + timedelta(seconds=30),
        )
    return worker_id


async def ingest_outbox(
    store: Store,
    settings: Settings,
    outbox: Outbox,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    worker_id: uuid.UUID | None = None,
    run_mode: str | None = None,
) -> IngestOutcome:
    """Apply every pending envelope of one session the way the API would."""
    resolved = worker_id or await lease_session(store, tenant_id, session_id)
    batcher = IngestBatcher()
    for envelope in outbox.pending(session_id):
        batcher.add(WorkerEnvelope.model_validate(envelope), 128)
    outcome = await flush_batch(
        store,
        batcher.take(),
        worker_id=resolved,
        settings=settings,
        metrics=None,
        run_mode=run_mode,
    )
    assert outcome.rejected == [], outcome.rejected
    return outcome


async def run_worker_turn(
    store: Store,
    settings: Settings,
    harness: object,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    text: str = "hello",
    **kwargs: Any,
) -> Outbox:
    """Run `run_turn` on an OutboxSink, then ingest the result into `store`."""
    context = await build_turn_context(store, settings, tenant_id, session_id)
    outbox = Outbox()
    await run_turn(
        EventHub(),
        cast(Harness, harness),
        tenant_id,
        session_id,
        text,
        settings=settings,
        turn_context=context,
        sink=OutboxSink(outbox, tenant_id, session_id),
        **kwargs,
    )
    await ingest_outbox(store, settings, outbox, tenant_id, session_id)
    return outbox
