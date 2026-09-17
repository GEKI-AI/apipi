from collections.abc import AsyncIterator
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.prom import metric_line

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FAKE_USAGE, FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _tenant(token: str) -> str:
    return str(uuid5(NAMESPACE_URL, hash_token(token)))


@pytest.fixture
def metrics_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        metrics=True,
    )


@pytest.fixture
async def metrics_client(
    metrics_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    app = create_app(metrics_settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_metrics_off_is_404(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 404


async def test_metrics_on_needs_no_bearer(metrics_client: AsyncClient) -> None:
    response = await metrics_client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "apipi_requests_total" in response.text or response.text.startswith("#")


async def test_scrape_after_turn_has_series_without_prompt(
    metrics_client: AsyncClient,
) -> None:
    token = "metrics-t"
    tenant = _tenant(token)
    agent = await metrics_client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await metrics_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    denied = await metrics_client.get("/v1/agents")
    assert denied.status_code == 401
    scrape = await metrics_client.get("/metrics")
    assert scrape.status_code == 200
    body = scrape.text
    assert "hello" not in body
    assert "secret-prompt" not in body
    assert session_id not in body
    assert token not in body
    assert metric_line(
        body, "apipi_turns_total", tenant=tenant, status="completed"
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant=tenant, kind="prompt"
    ).endswith(f" {float(FAKE_USAGE['prompt_tokens'])}")
    assert metric_line(
        body, "apipi_tokens_total", tenant=tenant, kind="completion"
    ).endswith(f" {float(FAKE_USAGE['completion_tokens'])}")
    assert metric_line(
        body, "apipi_tokens_total", tenant=tenant, kind="cache_read"
    ).endswith(f" {float(FAKE_USAGE['cache_read_tokens'])}")
    assert metric_line(
        body, "apipi_tokens_total", tenant=tenant, kind="cache_write"
    ).endswith(f" {float(FAKE_USAGE['cache_write_tokens'])}")
    assert metric_line(
        body, "apipi_tokens_total", tenant=tenant, kind="total"
    ).endswith(f" {float(FAKE_USAGE['total_tokens'])}")
    assert metric_line(
        body, "apipi_turn_latency_seconds_count", tenant=tenant
    ).endswith(" 1.0")
    assert metric_line(
        body,
        "apipi_requests_total",
        tenant=tenant,
        method="POST",
        path="/v1/agents/sessions",
        status="200",
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_errors_total", tenant="", code="unauthorized"
    ).endswith(" 1.0")
