import uuid
from datetime import UTC, datetime

from apipi.services.usage import (
    add_usage,
    empty_usage,
    usage_event,
    usage_from,
    usage_from_messages,
)


def test_usage_from_maps_pi_and_drops_extras() -> None:
    usage = usage_from(
        {
            "input": 5,
            "output": 8,
            "cacheRead": 1,
            "cacheWrite": 2,
            "totalTokens": 16,
            "cost": {"total": 0.3},
            "prompt": "secret-prompt",
        }
    )
    assert usage == {
        "prompt_tokens": 5,
        "completion_tokens": 8,
        "cache_read_tokens": 1,
        "cache_write_tokens": 2,
        "total_tokens": 16,
    }
    assert "cost" not in usage
    assert "prompt" not in usage
    assert "secret-prompt" not in str(usage)


def test_missing_counts_are_zero() -> None:
    assert usage_from({}) == empty_usage()
    assert usage_from(None) == empty_usage()
    assert usage_from({"input": 4}) == {
        "prompt_tokens": 4,
        "completion_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }


def test_usage_from_messages_sums_assistant_only() -> None:
    usage = usage_from_messages(
        [
            {"role": "user", "content": "hi", "usage": {"input": 99}},
            {
                "role": "assistant",
                "usage": {"input": 1, "output": 2, "totalTokens": 3},
            },
            {
                "role": "toolResult",
                "usage": {"input": 7, "output": 8, "totalTokens": 15},
            },
            {
                "role": "assistant",
                "usage": {
                    "input": 4,
                    "output": 5,
                    "cacheRead": 1,
                    "cacheWrite": 2,
                    "totalTokens": 12,
                },
            },
        ]
    )
    assert usage == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "cache_read_tokens": 1,
        "cache_write_tokens": 2,
        "total_tokens": 15,
    }


def test_add_usage() -> None:
    other = empty_usage()
    other["prompt_tokens"] = 1
    assert add_usage(empty_usage(), other)["prompt_tokens"] == 1


def test_usage_event_has_no_message_text() -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    event = usage_event(
        tenant_id=tenant_id,
        key_id="key-1",
        session_id=session_id,
        turn_id=turn_id,
        agent_id=None,
        model="test",
        status="completed",
        latency_ms=12,
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        tool_names=["echo"],
        tool_counts={"echo": 1},
        mcp_names=[],
        mcp_counts={},
        environment_type="none",
        run_mode="none",
        instance_id="node-a",
        artifact_bytes=0,
        request_id="req-1",
        error_code=None,
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    blob = str(event)
    assert "hello" not in blob
    assert "secret" not in blob
    assert event["tenant_id"] == str(tenant_id)
    assert event["prompt_tokens"] == 1
    assert event["environment_type"] == "none"
    assert "content" not in event
    assert "prompt" not in event
