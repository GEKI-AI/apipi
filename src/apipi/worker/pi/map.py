import uuid
from collections.abc import Callable
from time import monotonic
from typing import Any

from apipi.common.failures import classify_host_message, pi_payload
from apipi.common.usage import usage_from_messages

PREVIEW_CHARS = 100
THINKING_STARTED = "agent.session.turn.thinking.started"
THINKING_COMPLETED = "agent.session.turn.thinking.completed"
COMPACTION_STARTED = "agent.session.turn.compaction.started"
COMPACTION_COMPLETED = "agent.session.turn.compaction.completed"
RETRYING = "agent.session.turn.retrying"
RETRY_COMPLETED = "agent.session.turn.retry.completed"
WEB_SEARCH_TOOL = "web_search"
WEB_SEARCH_ITEM = "web_search_call"
WEB_SEARCH_QUERY_CHARS = 2000
WEB_SEARCH_ERROR_CHARS = 200


def _tool_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details")
    if isinstance(details, dict):
        return details
    result = event.get("result")
    if isinstance(result, dict):
        nested = result.get("details")
        if isinstance(nested, dict):
            return nested
    return {}


def _tool_item_type(name: object, details: dict[str, Any] | None = None) -> str:
    if name == WEB_SEARCH_TOOL:
        return WEB_SEARCH_ITEM
    if details:
        server = details.get("server")
        if isinstance(server, str) and server:
            return "mcp_call"
    if isinstance(name, str):
        lowered = name.lower()
        if lowered.startswith("mcp__") or lowered.startswith("mcp_"):
            return "mcp_call"
    return "command_execution"


def _tool_names(
    event: dict[str, Any], details: dict[str, Any]
) -> tuple[str, str | None]:
    server = details.get("server")
    tool = details.get("tool")
    name = event.get("toolName")
    if isinstance(server, str) and server and isinstance(tool, str) and tool:
        return tool, server
    if isinstance(name, str):
        return name, None
    return "", None


def _search_query(*sources: object) -> str | None:
    for source in sources:
        if isinstance(source, dict):
            query = source.get("query")
            if isinstance(query, str) and query.strip():
                return query.strip()[:WEB_SEARCH_QUERY_CHARS]
    return None


def _search_action(query: str | None) -> dict[str, Any]:
    action: dict[str, Any] = {"type": "search"}
    if query is not None:
        action["query"] = query
    return action


def _error_text(result: object) -> str:
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text = " ".join(part["text"].split())
                    if text:
                        return text[:WEB_SEARCH_ERROR_CHARS]
    return "Web search failed"


def _host_error(raw: object) -> list[tuple[str, dict[str, Any]]]:
    return [("pi_error", pi_payload(classify_host_message(raw)))]


def map_pi_event(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    kind = event.get("type")
    if kind == "agent_end":
        messages = event.get("messages")
        mapped: list[tuple[str, dict[str, Any]]] = []
        usage = usage_from_messages(messages)
        if usage is not None:
            mapped.append(("usage", usage))
        if (
            isinstance(messages, list)
            and messages
            and event.get("willRetry") is not True
        ):
            last = messages[-1]
            if isinstance(last, dict) and last.get("stopReason") == "error":
                mapped.extend(_host_error(last.get("errorMessage")))
        return mapped
    if kind == "auto_retry_start":
        return [(RETRYING, _retry_start(event))]
    if kind == "auto_retry_end":
        return [(RETRY_COMPLETED, _retry_end(event))]
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
        details = _tool_details(event)
        parent = event.get("parentToolCallId")
        call_id = event.get("toolCallId")
        name, server_label = _tool_names(event, details)
        item_type = _tool_item_type(name, details)
        data: dict[str, Any] = {
            "item_type": item_type,
            "call_id": call_id,
            "name": name,
        }
        if server_label is not None:
            data["server_label"] = server_label
        if item_type == WEB_SEARCH_ITEM:
            data["status"] = "in_progress"
            data["action"] = _search_action(_search_query(event.get("args")))
        if isinstance(parent, str) and parent:
            data["parent_call_id"] = parent
            return [("agent.session.turn.item.nested", data)]
        return [("agent.session.turn.item.added", data)]
    if kind == "tool_execution_end":
        details = _tool_details(event)
        parent = event.get("parentToolCallId")
        call_id = event.get("toolCallId")
        name, server_label = _tool_names(event, details)
        item_type = _tool_item_type(name, details)
        data = {
            "item_type": item_type,
            "call_id": call_id,
            "is_error": bool(event.get("isError")),
        }
        if server_label is not None:
            data["server_label"] = server_label
            data["name"] = name
        if item_type == WEB_SEARCH_ITEM:
            data["name"] = name
            data["action"] = _search_action(_search_query(details, event.get("args")))
            if event.get("isError"):
                data["status"] = "failed"
                data["error"] = _error_text(event.get("result"))
            else:
                data["status"] = "completed"
        if isinstance(parent, str) and parent:
            data["parent_call_id"] = parent
            return [("agent.session.turn.item.nested", data)]
        return [("agent.session.turn.item.done", data)]
    if kind == "tool_execution_update":
        details = _tool_details(event)
        if not details:
            return []
        call_id = event.get("toolCallId")
        name, server_label = _tool_names(event, details)
        if server_label is None:
            return []
        data = {
            "item_type": _tool_item_type(name, details),
            "call_id": call_id,
            "name": name,
            "server_label": server_label,
        }
        parent = event.get("parentToolCallId")
        if isinstance(parent, str) and parent:
            data["parent_call_id"] = parent
            return [("agent.session.turn.item.nested", data)]
        return [
            (
                "agent.session.turn.item.added",
                data,
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


def _retry_start(event: dict[str, Any]) -> dict[str, Any]:
    failure = classify_host_message(event.get("errorMessage"))
    attempt = _optional_int(event.get("attempt"))
    max_attempts = _optional_int(event.get("maxAttempts"))
    delay_ms = _optional_int(event.get("delayMs"))
    return {
        "attempt": attempt if attempt is not None else 0,
        "max_attempts": max_attempts if max_attempts is not None else 0,
        "delay_ms": delay_ms if delay_ms is not None else 0,
        "code": failure.code,
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
    }


def _retry_end(event: dict[str, Any]) -> dict[str, Any]:
    attempt = _optional_int(event.get("attempt"))
    return {
        "success": event.get("success") is True,
        "attempts": attempt if attempt is not None else 0,
    }


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
        self._reset()
        return [(THINKING_COMPLETED, payload)]

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
        self._reset()
        return [(THINKING_COMPLETED, payload)]

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

    def _reset(self) -> None:
        self._item_id = None
        self._content_index = 0
        self._started = None
        self._parts = []
