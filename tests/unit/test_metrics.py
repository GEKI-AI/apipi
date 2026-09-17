from tests.support.prom import metric_line

from apipi.gateway.metrics import Metrics


def test_observe_turn_series_have_no_prompt_text() -> None:
    metrics = Metrics()
    metrics.observe_turn(
        tenant="tenant-1",
        status="completed",
        latency_ms=40,
        prompt_tokens=11,
        completion_tokens=7,
        cache_read_tokens=3,
        cache_write_tokens=2,
        total_tokens=23,
    )
    body = metrics.scrape().decode()
    assert "secret-prompt" not in body
    assert "hello" not in body
    assert metric_line(
        body, "apipi_turns_total", tenant="tenant-1", status="completed"
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant="tenant-1", kind="prompt"
    ).endswith(" 11.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant="tenant-1", kind="completion"
    ).endswith(" 7.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant="tenant-1", kind="cache_read"
    ).endswith(" 3.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant="tenant-1", kind="cache_write"
    ).endswith(" 2.0")
    assert metric_line(
        body, "apipi_tokens_total", tenant="tenant-1", kind="total"
    ).endswith(" 23.0")
    assert metric_line(
        body, "apipi_turn_latency_seconds_count", tenant="tenant-1"
    ).endswith(" 1.0")
    assert "apipi_errors_total{" not in body


def test_observe_request_errors_without_prompt() -> None:
    metrics = Metrics()
    metrics.observe_request(
        tenant="",
        method="GET",
        path="/v1/agents",
        status=401,
        error_code="unauthorized",
    )
    body = metrics.scrape().decode()
    assert "secret-prompt" not in body
    assert metric_line(
        body,
        "apipi_requests_total",
        tenant="",
        method="GET",
        path="/v1/agents",
        status="401",
    ).endswith(" 1.0")
    assert metric_line(
        body, "apipi_errors_total", tenant="", code="unauthorized"
    ).endswith(" 1.0")
