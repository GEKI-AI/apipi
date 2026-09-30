import json

import pytest

from apipi.services.runtime import LIVE_EVENT_TYPES, PUBLIC_EVENT_TYPES
from apipi.worker.pi.map import (
    THINKING_COMPLETED,
    THINKING_STARTED,
    ThinkingTracker,
    map_pi_event,
)
from apipi.worker.pi.version import PINNED_PI


def test_pinned_pi() -> None:
    assert PINNED_PI == "0.99.1"


def test_text_delta_is_public() -> None:
    mapped = map_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "hi"},
        }
    )
    assert mapped == [("agent.session.turn.output_text.delta", {"delta": "hi"})]
    assert mapped[0][0] in PUBLIC_EVENT_TYPES


def test_text_end_is_public() -> None:
    mapped = map_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_end", "content": "hi"},
        }
    )
    assert mapped == [("agent.session.turn.output_text.done", {"text": "hi"})]


def test_agent_end_error_is_model_host_failure() -> None:
    mapped = map_pi_event(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": (
                        "OpenAI API error (401): "
                        '{"message":"Incorrect API key provided: secret"}'
                    ),
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "totalTokens": 0,
                    },
                }
            ],
        }
    )
    kinds = [item[0] for item in mapped]
    assert "pi_error" in kinds
    error = next(item[1] for item in mapped if item[0] == "pi_error")
    assert error["message"] == "Model host error (401)"
    assert error["code"] == "upstream_unauthorized"
    assert error["upstream_status"] == 401
    assert "secret" not in error["message"]


def test_agent_end_error_passes_plain_message() -> None:
    mapped = map_pi_event(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "No model configured for provider",
                }
            ],
        }
    )
    error = next(item[1] for item in mapped if item[0] == "pi_error")
    assert error["message"] == "No model configured for provider"
    assert error["code"] == "upstream_error"
    assert error["upstream_status"] is None


def test_extension_error_is_logged_in_full(caplog: pytest.LogCaptureFixture) -> None:
    from apipi.worker.pi.proc import log_extension_error

    detail = "mcp attach failed: " + ("x" * 800)
    with caplog.at_level("WARNING", logger="apipi.worker.pi"):
        log_extension_error(
            {
                "type": "extension_error",
                "error": detail,
                "stack": "Error: mcp attach failed\n    at attachStdio",
            }
        )
    record = caplog.records[-1]
    assert record.getMessage() == "pi extension error"
    logged = record.__dict__["error"]
    assert detail in logged
    assert "at attachStdio" in logged


def test_internal_pi_events_are_dropped() -> None:
    assert map_pi_event({"type": "agent_start"}) == []
    assert map_pi_event({"type": "turn_start"}) == []
    assert map_pi_event({"type": "extension_error", "error": "x"}) == []
    assert map_pi_event({"type": "agent_end", "messages": []}) == []


def test_agent_end_maps_usage_without_cost_or_text() -> None:
    mapped = map_pi_event(
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "usage": {
                        "input": 5,
                        "output": 8,
                        "cacheRead": 1,
                        "cacheWrite": 2,
                        "totalTokens": 16,
                        "cost": {"total": 0.3},
                        "prompt": "secret-prompt",
                    },
                }
            ],
        }
    )
    assert mapped == [
        (
            "usage",
            {
                "prompt_tokens": 5,
                "completion_tokens": 8,
                "cache_read_tokens": 1,
                "cache_write_tokens": 2,
                "total_tokens": 16,
            },
        )
    ]
    payload = mapped[0][1]
    assert "cost" not in payload
    assert "prompt" not in payload
    assert mapped[0][0] not in PUBLIC_EVENT_TYPES


def test_bash_tool_is_command_execution() -> None:
    mapped = map_pi_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
        }
    )
    assert mapped[0][1]["item_type"] == "command_execution"
    assert mapped[0][1]["name"] == "bash"


def test_mcp_tool_is_mcp_call() -> None:
    mapped = map_pi_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "mcp_tavily_search",
        }
    )
    assert mapped[0][1]["item_type"] == "mcp_call"
    assert mapped[0][0] in PUBLIC_EVENT_TYPES


def test_mcp_details_give_original_names() -> None:
    mapped = map_pi_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "mcp__docs__a1b2c3d4",
            "details": {"server": "docs", "tool": "search"},
        }
    )
    assert mapped[0][1]["item_type"] == "mcp_call"
    assert mapped[0][1]["server_label"] == "docs"
    assert mapped[0][1]["name"] == "search"


def test_nested_calls_are_not_top_level() -> None:
    mapped = map_pi_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c2",
            "toolName": "bash",
            "parentToolCallId": "c1",
        }
    )
    assert mapped[0][0] == "agent.session.turn.item.nested"
    assert mapped[0][0] in PUBLIC_EVENT_TYPES
    assert mapped[0][1]["parent_call_id"] == "c1"


def test_codemode_is_command_execution() -> None:
    mapped = map_pi_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "codemode",
        }
    )
    assert mapped[0][1]["item_type"] == "command_execution"


def test_mapped_payload_has_no_pi_keys() -> None:
    mapped = map_pi_event(
        {
            "type": "message_update",
            "usage": {"input": 5, "prompt": "secret-prompt"},
            "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
        }
    )
    payload = mapped[0][1]
    assert "assistantMessageEvent" not in payload
    assert "type" not in payload
    assert "usage" not in payload
    assert "prompt" not in payload


def _thinking_update(
    inner: dict[str, object], reasoning: int | None = None
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "message_update",
        "assistantMessageEvent": inner,
    }
    if reasoning is not None:
        event["usage"] = {"reasoning": reasoning}
    return event


def test_thinking_preview_is_truncated_and_public() -> None:
    clock = iter([10.0, 10.25])
    tracker = ThinkingTracker(clock=lambda: next(clock))
    full = "你" * 100 + "TAIL-SECRET"
    started = tracker.feed(
        _thinking_update({"type": "thinking_start", "contentIndex": 1})
    )
    assert (
        tracker.feed(
            _thinking_update(
                {"type": "thinking_delta", "contentIndex": 1, "delta": full}
            )
        )
        == []
    )
    completed = tracker.feed(
        _thinking_update(
            {"type": "thinking_end", "contentIndex": 1, "content": full},
            reasoning=12,
        )
    )
    assert started[0][0] == THINKING_STARTED
    assert started[0][0] in PUBLIC_EVENT_TYPES
    assert started[0][0] not in LIVE_EVENT_TYPES
    assert completed[0][0] == THINKING_COMPLETED
    assert completed[0][0] in PUBLIC_EVENT_TYPES
    assert completed[0][0] not in LIVE_EVENT_TYPES
    payload = completed[0][1]
    assert payload["item_id"] == started[0][1]["item_id"]
    assert payload["content_index"] == 1
    assert payload["duration_ms"] == 250
    assert payload["reasoning_tokens"] == 12
    assert payload["preview"] == full[:100]
    assert payload["preview_truncated"] is True
    assert len(completed) == 1
    assert "TAIL-SECRET" not in json.dumps(completed)
    assert "thinking_body" not in PUBLIC_EVENT_TYPES


def test_thinking_end_without_start_has_null_duration() -> None:
    tracker = ThinkingTracker()
    completed = tracker.feed(
        _thinking_update(
            {"type": "thinking_end", "contentIndex": 0, "content": "short"}
        )
    )
    payload = completed[0][1]
    assert payload["duration_ms"] is None
    assert payload["reasoning_tokens"] is None
    assert payload["preview"] == "short"
    assert payload["preview_truncated"] is False


def test_thinking_deltas_are_not_public() -> None:
    assert (
        map_pi_event(
            _thinking_update({"type": "thinking_delta", "delta": "secret thinking"})
        )
        == []
    )


def test_compaction_events_are_public_without_summary() -> None:
    started = map_pi_event({"type": "compaction_start", "reason": "threshold"})
    assert started == [
        ("agent.session.turn.compaction.started", {"reason": "threshold"})
    ]
    assert started[0][0] in PUBLIC_EVENT_TYPES
    ended = map_pi_event(
        {
            "type": "compaction_end",
            "reason": "threshold",
            "aborted": False,
            "willRetry": False,
            "result": {
                "summary": "SECRET-SUMMARY",
                "tokensBefore": 150000,
                "estimatedTokensAfter": 32000,
            },
        }
    )
    assert ended[0][0] == "agent.session.turn.compaction.completed"
    assert ended[0][0] in PUBLIC_EVENT_TYPES
    assert ended[0][1]["tokens_before"] == 150000
    assert ended[0][1]["tokens_after"] == 32000
    assert "SECRET-SUMMARY" not in json.dumps(ended)
    assert map_pi_event({"type": "not_a_compaction"}) == []


def _error_end(message: str, *, will_retry: bool) -> dict[str, object]:
    return {
        "type": "agent_end",
        "willRetry": will_retry,
        "messages": [
            {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": message,
            }
        ],
    }


def test_will_retry_does_not_fail_the_turn() -> None:
    mapped = map_pi_event(_error_end("504 status code (no body)", will_retry=True))
    kinds = [item[0] for item in mapped]
    assert "pi_error" not in kinds
    assert "agent.session.turn.retrying" not in kinds


@pytest.mark.parametrize(
    ("text", "code", "status"),
    [
        ('504: {"error":{"message":"timeout"}}', "upstream_timeout", 504),
        ("504 status code (no body)", "upstream_timeout", 504),
        ("Connection error.", "upstream_connection", None),
        ("fetch failed", "upstream_connection", None),
        ("502: connection error", "upstream_5xx", 502),
        ("502 Bad Gateway", "upstream_5xx", 502),
        ("503 Service Unavailable", "upstream_5xx", 503),
        ("429 Rate limit reached for requests", "upstream_rate_limited", 429),
        ("Request timed out.", "upstream_timeout", None),
        ("400 Invalid parameter", "upstream_4xx", 400),
    ],
)
def test_pi_0851_retry_texts(text: str, code: str, status: int | None) -> None:
    mapped = map_pi_event(
        {
            "type": "auto_retry_start",
            "attempt": 1,
            "maxAttempts": 3,
            "delayMs": 2000,
            "errorMessage": text + " sk-secret",
        }
    )
    assert mapped[0][0] == "agent.session.turn.retrying"
    assert mapped[0][0] in PUBLIC_EVENT_TYPES
    data = mapped[0][1]
    assert data["attempt"] == 1
    assert data["max_attempts"] == 3
    assert data["delay_ms"] == 2000
    assert data["code"] == code
    assert data["failure_source"] == "upstream"
    assert data["upstream_status"] == status
    assert "sk-secret" not in json.dumps(data)
    assert "errorMessage" not in data


def test_retry_completed_is_public() -> None:
    mapped = map_pi_event(
        {
            "type": "auto_retry_end",
            "success": False,
            "attempt": 2,
            "finalError": "Retry cancelled",
        }
    )
    assert mapped == [
        (
            "agent.session.turn.retry.completed",
            {"success": False, "attempts": 2},
        )
    ]
    assert mapped[0][0] in PUBLIC_EVENT_TYPES
    assert "Retry cancelled" not in json.dumps(mapped)
