from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any
from uuid import UUID

from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Span, format_trace_id, get_current_span
from opentelemetry.util.types import AttributeValue

_ALLOWED = frozenset(
    {
        "request_id",
        "session_id",
        "turn_id",
        "model",
        "status",
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "tool_names",
    }
)
_IDS = frozenset({"request_id", "session_id", "turn_id", "model", "status"})
_TOKENS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
    }
)


def traces_endpoint(url: str) -> str:
    base = url.strip().rstrip("/")
    if base.endswith("/v1/traces"):
        return base
    return f"{base}/v1/traces"


def span_attributes(values: Mapping[str, object]) -> dict[str, AttributeValue]:
    out: dict[str, AttributeValue] = {}
    for key, value in values.items():
        if key not in _ALLOWED or value is None:
            continue
        if key == "tool_names":
            if isinstance(value, list | tuple):
                out[key] = tuple(item for item in value if isinstance(item, str))
            continue
        if key in _TOKENS:
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            out[key] = value
            continue
        if key in _IDS:
            if isinstance(value, UUID):
                out[key] = str(value)
            elif isinstance(value, str) and value:
                out[key] = value
    return out


class Tracing:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        exporter: SpanExporter | None = None,
    ) -> None:
        if exporter is None:
            if not endpoint:
                raise ValueError("APIPI_OTEL_ENDPOINT")
            exporter = OTLPSpanExporter(endpoint=traces_endpoint(endpoint))
            processor: SimpleSpanProcessor | BatchSpanProcessor = BatchSpanProcessor(
                exporter
            )
        else:
            processor = SimpleSpanProcessor(exporter)
        provider = TracerProvider(resource=Resource.create({"service.name": "apipi"}))
        provider.add_span_processor(processor)
        self._provider = provider
        self._tracer = provider.get_tracer("apipi")

    def span(self, name: str, **attrs: object) -> Any:
        return self._tracer.start_as_current_span(
            name,
            attributes=span_attributes(attrs),
            record_exception=False,
        )

    def set(self, span: Span | None = None, **attrs: object) -> None:
        target = span if span is not None else get_current_span()
        if not target.is_recording():
            return
        for key, value in span_attributes(attrs).items():
            target.set_attribute(key, value)

    def shutdown(self) -> None:
        self._provider.shutdown()


def current_trace_id() -> str | None:
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format_trace_id(context.trace_id)


def start_span(tracing: Tracing | None, name: str, **attrs: object) -> Any:
    if not isinstance(tracing, Tracing):
        return nullcontext(None)
    return tracing.span(name, **attrs)


def set_span(
    tracing: Tracing | None, span: Span | None = None, **attrs: object
) -> None:
    if not isinstance(tracing, Tracing):
        return
    tracing.set(span, **attrs)
