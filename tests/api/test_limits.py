"""Capacity limits through the split production path (#473).

API-side capacity in split mode comes from worker assignment: a worker
advertises `max_sessions` as its connection capacity, and the API
rejects a new turn with 429 `capacity` when no live connection has room
(`worker.assign.failed`). Per-tenant caps live one layer down, in the
worker pool at sandbox spawn time, and are unit-covered in
`tests/unit/test_pool.py::test_has_capacity_per_tenant`; there is no
HTTP-level per-tenant gate once a worker is live, so that case is
covered here as non-interference instead of a per-tenant 429.
"""

import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.split_worker import api_settings_for, split_client_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store


@pytest.fixture
def limited_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        max_sessions=1,
        max_request_bytes=1024,
    )


async def test_capacity_rejects_new_turn(
    limited_settings: Settings,
    store: Store,
    worker_secret: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A worker at its advertised capacity rejects the next turn with 429."""
    caplog.set_level(logging.WARNING, logger="apipi")
    token = {"Authorization": "Bearer t"}
    async with split_client_for(limited_settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        agent = await client.post(
            "/v1/agents", headers=token, json={"name": "bot", "model": "test"}
        )
        assert agent.status_code == 200
        # The worker advertises capacity 1; the finished turn keeps its
        # lease, so the worker is full for the next session.
        first = await client.post(
            "/v1/agents/sessions",
            headers=token,
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
        assert first.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers=token,
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
    assert created.status_code == 429
    error = created.json()["error"]
    assert error["code"] == "capacity"
    assigned = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "worker.assign.failed"
    ]
    assert assigned
    assert assigned[-1].__dict__["error_code"] == "capacity"
    assert assigned[-1].__dict__.get("session_id")
    assert assigned[-1].__dict__.get("tenant_id")


async def test_other_tenant_unaffected_by_occupied_lease(
    store: Store, tmp_path: Path, worker_secret: str
) -> None:
    """Below the worker's capacity, one tenant's lease never blocks another."""
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        max_sessions=8,
    )
    async with split_client_for(settings, store, token=worker_secret) as (
        _app,
        client,
        _worker,
    ):
        first = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer a"},
            json={"name": "bot", "model": "test"},
        )
        assert first.status_code == 200
        held = await client.post(
            "/v1/agents/sessions",
            headers={"Authorization": "Bearer a"},
            json={
                "agent_id": first.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
        assert held.status_code == 200
        other = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer b"},
            json={"name": "bot", "model": "test"},
        )
        assert other.status_code == 200
        ok = await client.post(
            "/v1/agents/sessions",
            headers={"Authorization": "Bearer b"},
            json={
                "agent_id": other.json()["id"],
                "environment": {"type": "none"},
                "input": "Hello",
            },
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "idle"


async def test_payload_too_large(limited_settings: Settings, store: Store) -> None:
    app = create_app(api_settings_for(limited_settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agents",
            headers={"Authorization": "Bearer t"},
            content=b"x" * 2048,
        )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
