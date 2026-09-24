import uuid
from collections.abc import Callable
from time import monotonic
from typing import Any

from apipi.services.usage import usage_from_messages

PREVIEW_CHARS = 100
THINKING_STARTED = "agent.session.turn.thinking.started"
THINKING_COMPLETED = "agent.session.turn.thinking.completed"
COMPACTION_STARTED = "agent.session.turn.compaction.started"
COMPACTION_COMPLETED = "agent.session.turn.compaction.completed"


def _tool_item_type(name: object) -> str:
    if isinstance(name, str) and name.lower().startswith("mcp"):
        return "mcp_call"
    return "command_execution"


def _host_error(raw: object) -> list[tuple[str, dict[str, Any]]]:
    text = raw if isinstance(raw, str) else ""
    if "(401)" in text:
        message = "Model host error (401)"
    elif "(403)" in text:
        message = "Model host error (403)"
    elif text.strip():
        message = text.strip().split("\n")[0][:300]
        lowered = message.lower()
        if "api key" in lowered or "bearer " in lowered:
            message = "Model host error"
    else:
        message = "Model host error"
    return [("pi_error", {"message": message})]


def map_pi_event(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    kind = event.get("type")
    if kind == "agent_end":
        messages = event.get("messages")
        mapped: list[tuple[str, dict[str, Any]]] = []
        usage = usage_from_messages(messages)
        if usage is not None:
            mapped.append(("usage", usage))
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, dict) and last.get("stopReason") == "error":
                mapped.extend(_host_error(last.get("errorMessage")))
        return mapped
    if kind == "message_update":
        delta = event.get("assistantMessageEvent")
        if not isinstance(delta, dict):
            return []
        inner = delta.get("type")
        if inner == "text_delta":
            text = delta.get("delta")
            if not isinstance(text, str):
                text = ""
            return [("agent.session.turn.output_text.delta", {"delta": text})]
        if inner == "text_end":
            text = delta.get("content")
            if not isinstance(text, str):
                text = ""
            return [("agent.session.turn.output_text.done", {"text": text})]
        return []
    if kind == "compaction_start":
        return [(COMPACTION_STARTED, _compaction_start(event))]
    if kind == "compaction_end":
        return [(COMPACTION_COMPLETED, _compaction_end(event))]
    if kind == "tool_execution_start":
        call_id = event.get("toolCallId")
        name = event.get("toolName")
        item_type = _tool_item_type(name)
        return [
            (
                "agent.session.turn.item.added",
                {
                    "item_type": item_type,
                    "call_id": call_id,
                    "name": name,
                },
            )
        ]
    if kind == "tool_execution_end":
        call_id = event.get("toolCallId")
        name = event.get("toolName")
        return [
            (
                "agent.session.turn.item.done",
                {
                    "item_type": _tool_item_type(name),
                    "call_id": call_id,
                    "is_error": bool(event.get("isError")),
                },
            )
        ]
    return []


def _optional_str(value: object, *, limit: int | None = None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if limit is not None and len(value) > limit:
        return value[:limit]
    return value


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _compaction_start(event: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    reason = _optional_str(event.get("reason"))
    if reason is not None:
        data["reason"] = reason
    return data


def _compaction_end(event: dict[str, Any]) -> dict[str, Any]:
    data = _compaction_start(event)
    if event.get("aborted") is True:
        data["aborted"] = True
    if event.get("willRetry") is True:
        data["will_retry"] = True
    error = _optional_str(event.get("errorMessage"), limit=300)
    if error is not None:
        data["error"] = error
    result = event.get("result")
    if isinstance(result, dict):
        tokens_before = _optional_int(result.get("tokensBefore"))
        tokens_after = _optional_int(result.get("estimatedTokensAfter"))
        if tokens_before is not None:
            data["tokens_before"] = tokens_before
        if tokens_after is not None:
            data["tokens_after"] = tokens_after
    return data


def _preview(text: str) -> tuple[str, bool]:
    if len(text) <= PREVIEW_CHARS:
        return text, False
    return text[:PREVIEW_CHARS], True


def _content_index(delta: dict[str, Any]) -> int:
    raw = delta.get("contentIndex")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _reasoning_tokens(event: dict[str, Any]) -> int | None:
    usage = event.get("usage")
    if not isinstance(usage, dict):
        return None
    raw = usage.get("reasoning")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


class ThinkingTracker:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or monotonic
        self._item_id: str | None = None
        self._content_index = 0
        self._started: float | None = None
        self._parts: list[str] = []

    def feed(self, event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        if event.get("type") != "message_update":
            return []
        delta = event.get("assistantMessageEvent")
        if not isinstance(delta, dict):
            return []
        inner = delta.get("type")
        if inner == "thinking_start":
            closed = self._finish_open()
            return [*closed, self._start(delta)]
        if inner == "thinking_delta":
            if self._item_id is None:
                return []
            text = delta.get("delta")
            if isinstance(text, str):
                self._parts.append(text)
            return []
        if inner == "thinking_end":
            return self._complete(event, delta)
        return []

    def _start(self, delta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        self._item_id = str(uuid.uuid4())
        self._content_index = _content_index(delta)
        self._started = self._clock()
        self._parts = []
        return (
            THINKING_STARTED,
            {"item_id": self._item_id, "content_index": self._content_index},
        )

    def _finish_open(self) -> list[tuple[str, dict[str, Any]]]:
        if self._item_id is None:
            return []
        text = "".join(self._parts)
        preview, truncated = _preview(text)
        payload = self._completed_payload(
            preview=preview,
            truncated=truncated,
            reasoning_tokens=None,
        )
        body = self._body(text)
        self._reset()
        return [(THINKING_COMPLETED, payload), body]

    def _complete(
        self, event: dict[str, Any], delta: dict[str, Any]
    ) -> list[tuple[str, dict[str, Any]]]:
        content = delta.get("content")
        text = content if isinstance(content, str) else "".join(self._parts)
        if self._item_id is None:
            self._item_id = str(uuid.uuid4())
            self._content_index = _content_index(delta)
            self._started = None
        preview, truncated = _preview(text)
        payload = self._completed_payload(
            preview=preview,
            truncated=truncated,
            reasoning_tokens=_reasoning_tokens(event),
        )
        body = self._body(text)
        self._reset()
        return [(THINKING_COMPLETED, payload), body]

    def _completed_payload(
        self,
        *,
        preview: str,
        truncated: bool,
        reasoning_tokens: int | None,
    ) -> dict[str, Any]:
        duration_ms = None
        if self._started is not None:
            duration_ms = max(0, round((self._clock() - self._started) * 1000))
        return {
            "item_id": self._item_id,
            "content_index": self._content_index,
            "duration_ms": duration_ms,
            "reasoning_tokens": reasoning_tokens,
            "preview": preview,
            "preview_truncated": truncated,
        }

    def _body(self, text: str) -> tuple[str, dict[str, Any]]:
        return ("thinking_body", {"item_id": self._item_id, "text": text})

    def _reset(self) -> None:
        self._item_id = None
        self._content_index = 0
        self._started = None
        self._parts = []
