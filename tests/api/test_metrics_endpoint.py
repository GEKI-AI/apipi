from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from tests.support.http import auth, tenant_of
from tests.support.prom import metric_line

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.store.engine import Store
from apipi.worker.fake_harness import FAKE_USAGE


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
    metrics_settings: Settings, store: Store, worker_secret: str
) -> AsyncIterator[tuple[AsyncClient, Metrics]]:
    """Split API client plus the worker-side metrics registry.

    Turn/token series are recorded where the turn runs (the worker);
    request/error series stay on the API scrape.
    """
    from tests.support.split_worker import split_client_for

    from apipi.common.metrics import Metrics

    worker_metrics = Metrics()
    async with split_client_for(
        metrics_settings, store, token=worker_secret, metrics=worker_metrics
    ) as (_app, client, _worker):
        yield client, worker_metrics


async def test_metrics_off_is_404(client: AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 404


async def test_metrics_on_needs_no_bearer(
    metrics_client: tuple[AsyncClient, Metrics],
) -> None:
    client, _worker_metrics = metrics_client
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "apipi_requests_total" in response.text or response.text.startswith("#")


async def test_scrape_after_turn_has_series_without_prompt(
    metrics_client: tuple[AsyncClient, Metrics],
) -> None:
    client, worker_metrics = metrics_client
    token = "metrics-t"
    tenant = str(tenant_of(token))
    agent = await client.post(
        "/v1/agents", headers=auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "private-prompt-text",
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    denied = await client.get("/v1/agents")
    assert denied.status_code == 401
    scrape = await client.get("/metrics")
    assert scrape.status_code == 200
    body = scrape.text
    assert "private-prompt-text" not in body
    assert "secret-prompt" not in body
    assert session_id not in body
    assert token not in body
    # Turn and token series are recorded where the turn runs.
    worker_body = worker_metrics.scrape().decode()
    assert "private-prompt-text" not in worker_body
    assert "secret-prompt" not in worker_body
    assert metric_line(
        worker_body, "apipi_turns_total", tenant=tenant, status="completed"
    ).endswith(" 1.0")
    assert metric_line(
        worker_body, "apipi_tokens_total", tenant=tenant, kind="prompt"
    ).endswith(f" {float(FAKE_USAGE['prompt_tokens'])}")
    assert metric_line(
        worker_body, "apipi_tokens_total", tenant=tenant, kind="completion"
    ).endswith(f" {float(FAKE_USAGE['completion_tokens'])}")
    assert metric_line(
        worker_body, "apipi_tokens_total", tenant=tenant, kind="cache_read"
    ).endswith(f" {float(FAKE_USAGE['cache_read_tokens'])}")
    assert metric_line(
        worker_body, "apipi_tokens_total", tenant=tenant, kind="cache_write"
    ).endswith(f" {float(FAKE_USAGE['cache_write_tokens'])}")
    assert metric_line(
        worker_body, "apipi_tokens_total", tenant=tenant, kind="total"
    ).endswith(f" {float(FAKE_USAGE['total_tokens'])}")
    assert metric_line(
        worker_body, "apipi_turn_latency_seconds_count", tenant=tenant
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
