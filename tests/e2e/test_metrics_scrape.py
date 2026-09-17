import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store

pytestmark = pytest.mark.e2e

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"
_MAPPED_PI_USAGE = {
    "prompt_tokens": 5,
    "completion_tokens": 8,
    "cache_read_tokens": 1,
    "cache_write_tokens": 2,
    "total_tokens": 16,
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def metrics_host_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        pi_command=f"{sys.executable} {_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
        metrics=True,
    )


@pytest.fixture
async def metrics_host_client(
    metrics_host_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    app = create_app(metrics_host_settings, store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


async def test_scrape_after_host_turn(metrics_host_client: AsyncClient) -> None:
    token = "e2e-metrics"
    tenant = str(uuid5(NAMESPACE_URL, hash_token(token)))
    created_agent = await metrics_host_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await metrics_host_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created_agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello-host",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    denied = await metrics_host_client.get("/v1/agents")
    assert denied.status_code == 401
    scrape = await metrics_host_client.get("/metrics")
    assert scrape.status_code == 200
    body = scrape.text
    assert "hello-host" not in body
    assert "secret-prompt" not in body
    assert session_id not in body
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
