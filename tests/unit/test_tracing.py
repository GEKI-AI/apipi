from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from apipi.gateway.otel import Tracing, span_attributes, traces_endpoint
from apipi.services.runtime import FAKE_USAGE


def test_traces_endpoint_appends_path() -> None:
    assert traces_endpoint("http://otel:4318") == "http://otel:4318/v1/traces"
    assert traces_endpoint("http://otel:4318/") == "http://otel:4318/v1/traces"
    assert traces_endpoint("http://otel:4318/v1/traces") == "http://otel:4318/v1/traces"


def test_span_attributes_keep_links_and_drop_message_text() -> None:
    attrs = span_attributes(
        {
            "request_id": "req-1",
            "session_id": "sess-1",
            "turn_id": "turn-1",
            "model": "test",
            "status": "completed",
            "prompt_tokens": FAKE_USAGE["prompt_tokens"],
            "completion_tokens": FAKE_USAGE["completion_tokens"],
            "cache_read_tokens": FAKE_USAGE["cache_read_tokens"],
            "cache_write_tokens": FAKE_USAGE["cache_write_tokens"],
            "total_tokens": FAKE_USAGE["total_tokens"],
            "tool_names": ["echo"],
            "text": "hello",
            "prompt": "secret-prompt",
            "content": "hello",
            "message": "hello",
        }
    )
    assert attrs["request_id"] == "req-1"
    assert attrs["session_id"] == "sess-1"
    assert attrs["turn_id"] == "turn-1"
    assert attrs["model"] == "test"
    assert attrs["status"] == "completed"
    assert attrs["prompt_tokens"] == FAKE_USAGE["prompt_tokens"]
    assert attrs["completion_tokens"] == FAKE_USAGE["completion_tokens"]
    assert attrs["cache_read_tokens"] == FAKE_USAGE["cache_read_tokens"]
    assert attrs["cache_write_tokens"] == FAKE_USAGE["cache_write_tokens"]
    assert attrs["total_tokens"] == FAKE_USAGE["total_tokens"]
    assert attrs["tool_names"] == ("echo",)
    blob = str(attrs)
    assert "hello" not in blob
    assert "secret-prompt" not in blob
    assert "text" not in attrs
    assert "prompt" not in attrs
    assert "content" not in attrs
    assert "message" not in attrs


def test_in_memory_exporter_records_nested_spans() -> None:
    exporter = InMemorySpanExporter()
    tracing = Tracing(exporter=exporter)
    try:
        with (
            tracing.span("session", request_id="req-1", session_id="sess-1"),
            tracing.span(
                "turn",
                request_id="req-1",
                session_id="sess-1",
                turn_id="turn-1",
                model="test",
            ),
        ):
            with tracing.span(
                "model",
                request_id="req-1",
                session_id="sess-1",
                turn_id="turn-1",
                model="test",
            ) as span:
                tracing.set(
                    span,
                    status="completed",
                    prompt_tokens=FAKE_USAGE["prompt_tokens"],
                    completion_tokens=FAKE_USAGE["completion_tokens"],
                    cache_read_tokens=FAKE_USAGE["cache_read_tokens"],
                    cache_write_tokens=FAKE_USAGE["cache_write_tokens"],
                    total_tokens=FAKE_USAGE["total_tokens"],
                )
            tracing.set(
                status="completed",
                tool_names=["echo"],
                prompt_tokens=FAKE_USAGE["prompt_tokens"],
                completion_tokens=FAKE_USAGE["completion_tokens"],
                cache_read_tokens=FAKE_USAGE["cache_read_tokens"],
                cache_write_tokens=FAKE_USAGE["cache_write_tokens"],
                total_tokens=FAKE_USAGE["total_tokens"],
            )
    finally:
        tracing.shutdown()
    spans = list(exporter.get_finished_spans())
    by_name = {span.name: span for span in spans}
    assert set(by_name) == {"session", "turn", "model"}
    session = by_name["session"]
    turn = by_name["turn"]
    model = by_name["model"]
    assert session.parent is None
    assert turn.parent is not None
    assert turn.parent.span_id == session.context.span_id
    assert model.parent is not None
    assert model.parent.span_id == turn.context.span_id
    session_attrs = dict(session.attributes or {})
    turn_attrs = dict(turn.attributes or {})
    model_attrs = dict(model.attributes or {})
    assert session_attrs["request_id"] == "req-1"
    assert session_attrs["session_id"] == "sess-1"
    assert turn_attrs["request_id"] == "req-1"
    assert turn_attrs["session_id"] == "sess-1"
    assert turn_attrs["turn_id"] == "turn-1"
    assert turn_attrs["model"] == "test"
    assert turn_attrs["status"] == "completed"
    assert turn_attrs["tool_names"] == ("echo",)
    assert turn_attrs["prompt_tokens"] == FAKE_USAGE["prompt_tokens"]
    assert model_attrs["request_id"] == "req-1"
    assert model_attrs["session_id"] == "sess-1"
    assert model_attrs["turn_id"] == "turn-1"
    assert model_attrs["status"] == "completed"
    blob = str(session_attrs) + str(turn_attrs) + str(model_attrs)
    assert "hello" not in blob
    assert "secret-prompt" not in blob
