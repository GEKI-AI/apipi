import uuid
from pathlib import Path

import pytest
from tests.support.http import auth, tenant_of
from tests.support.procs import split_http_client
from tests.support.prom import metric_line

from apipi.store.engine import Store
from apipi.store.repo import get_session_turn

# Real `apipi serve` + `apipi worker` processes (run mode `none`, fake Pi).
# Hosted setup, sandbox TTL wipes, and pool reaping are covered in
# tests/api/test_hosted_setup.py, and the microvm worker in
# tests/e2e/test_microvm_pi.py.
pytestmark = pytest.mark.e2e

_MAPPED_PI_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 8,
    "cache_read_tokens": 1,
    "cache_write_tokens": 2,
    "total_tokens": 16,
}


async def test_none_turn_persists_usage_and_counts_metrics(
    store: Store, tmp_path: Path
) -> None:
    token = "e2e-none"
    tenant_id = tenant_of(token)
    async with split_http_client(
        store, tmp_path, api_env={"APIPI_METRICS": "1"}
    ) as client:
        created_agent = await client.post(
            "/v1/agents",
            headers=auth(token),
            json={"name": "bot", "model": "test"},
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={
                "agent_id": created_agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello-none",
            },
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["id"]
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
        )
        completed = [
            event
            for event in events.json()["data"]
            if event["type"] == "agent.session.turn.completed"
        ]
        done = [
            event
            for event in events.json()["data"]
            if event["type"] == "agent.session.turn.output_text.done"
        ]
        assert done[0]["data"]["text"] == "hello-none"
        assert "assistantMessageEvent" not in done[0]["data"]
        assert len(completed) == 1
        usage = completed[0]["data"]["usage"]
        assert usage == _MAPPED_PI_USAGE
        assert "cost" not in usage
        assert "prompt" not in usage
        assert "secret-prompt" not in str(usage)
        turn_id = completed[0]["data"]["turn_id"]
        one = await client.get(
            f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
            headers=auth(token),
        )
        assert one.json()["usage"] == _MAPPED_PI_USAGE
        denied = await client.get("/v1/agents")
        assert denied.status_code == 401
        scrape = await client.get("/metrics")
        async with store.session() as db:
            row = await get_session_turn(
                db, tenant_id, uuid.UUID(session_id), uuid.UUID(turn_id)
            )
        assert row is not None
        assert row.usage == _MAPPED_PI_USAGE
        assert scrape.status_code == 200
        body = scrape.text
        assert "hello-none" not in body
        assert "secret-prompt" not in body
        assert session_id not in body
        tenant = str(tenant_id)
        assert metric_line(
            body, "apipi_turns_total", tenant=tenant, status="completed"
        ).endswith(" 1.0")
        assert metric_line(
            body, "apipi_tokens_total", tenant=tenant, kind="prompt"
        ).endswith(f" {float(_MAPPED_PI_USAGE['prompt_tokens'])}")
        assert metric_line(
            body, "apipi_tokens_total", tenant=tenant, kind="total"
        ).endswith(f" {float(_MAPPED_PI_USAGE['total_tokens'])}")
        assert metric_line(
            body, "apipi_turn_latency_seconds_count", tenant=tenant
        ).endswith(" 1.0")
        assert "apipi_requests_total{" in body
        assert metric_line(
            body, "apipi_errors_total", tenant="", code="unauthorized"
        ).endswith(" 1.0")
