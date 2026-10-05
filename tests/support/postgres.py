"""Tests against a live Postgres.

They need a migrated database (``apipi migrate`` against
``APIPI_TEST_DATABASE_URL``) and are marked slow, so GitHub CI skips
them. Run them locally with e.g.::

    export APIPI_TEST_DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
    uv run pytest -m slow -k postgres tests/unit
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from apipi.common.metrics import Metrics
from apipi.services.event_bus import PostgresEventBus

PG_URL = os.environ.get("APIPI_TEST_DATABASE_URL")

needs_postgres = [
    pytest.mark.slow,
    pytest.mark.skipif(not PG_URL, reason="needs APIPI_TEST_DATABASE_URL"),
]


def pg_dsn() -> str:
    assert PG_URL is not None
    return PG_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def postgres_replicas(
    metrics: Metrics | None = None,
) -> AsyncIterator[tuple[PostgresEventBus, PostgresEventBus]]:
    replica_a = PostgresEventBus(pg_dsn(), metrics=metrics)
    replica_b = PostgresEventBus(pg_dsn(), metrics=metrics)
    await replica_a.start()
    try:
        await replica_b.start()
        try:
            yield replica_a, replica_b
        finally:
            await replica_b.close()
    finally:
        await replica_a.close()
