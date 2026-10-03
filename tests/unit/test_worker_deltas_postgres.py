"""Delta relay across two API replicas over Postgres LISTEN/NOTIFY.

Needs a live, migrated Postgres (``apipi migrate`` against
``APIPI_TEST_DATABASE_URL``) and is marked slow, so GitHub CI skips
it. Run locally with e.g.::

    export APIPI_TEST_DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
    uv run pytest -m slow tests/unit/test_delta_relay_postgres.py
"""

import asyncio
import os
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from apipi.config import Settings
from apipi.protocol import WorkerEnvelope
from apipi.services.event_bus import PostgresEventBus
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import create_session, create_tenant, set_session_lease
from apipi.workerhub.connection import WorkerConnection
from apipi.workerhub.hub import WorkerHub

PG_URL = os.environ.get("APIPI_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not PG_URL, reason="needs APIPI_TEST_DATABASE_URL"),
]


def _dsn() -> str:
    assert PG_URL is not None
    return PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


async def test_delta_reaches_sse_on_other_replica(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    hub = WorkerHub(
        Settings(
            database_url=settings.database_url,
            run_mode="none",
            sessions_dir=str(tmp_path / "sessions"),
        )
    )
    replica_a = PostgresEventBus(_dsn())
    replica_b = PostgresEventBus(_dsn())
    await replica_a.start()
    await replica_b.start()
    try:
        worker_id = uuid.uuid4()
        async with store.session() as db:
            tenant = await create_tenant(db, name="t")
            session_row = await create_session(db, tenant.id)
            lease_id = uuid.uuid4()
            await set_session_lease(
                db,
                tenant.id,
                session_row.id,
                worker_id=worker_id,
                lease_id=lease_id,
                lease_until=utc_now() + timedelta(minutes=5),
            )
            session_id = session_row.id
        conn = WorkerConnection(
            worker_id=worker_id,
            generation=1,
            websocket=cast(Any, None),
            capacity=2,
            memory_mb=512,
            run_mode="none",
            leases={lease_id},
        )
        queue_b = replica_b.subscribe(session_id)
        try:
            turn_id = uuid.uuid4()
            published = await hub.handle_delta(
                store,
                replica_a,
                conn,
                WorkerEnvelope.model_validate(
                    {
                        "v": 2,
                        "session_id": str(session_id),
                        "turn_id": str(turn_id),
                        "seq": 1,
                        "type": "delta.text",
                        "payload": {"turn_id": str(turn_id), "text": "hello"},
                    }
                ),
            )
            assert published is True
            async with asyncio.timeout(15):
                message = await queue_b.get()
            assert message["type"] == "agent.session.turn.output_text.delta"
            assert message["data"] == {"delta": "hello", "turn_id": str(turn_id)}
        finally:
            replica_b.unsubscribe(session_id, queue_b)
    finally:
        await replica_a.close()
        await replica_b.close()
