from apipi.services.runtime import PUBLIC_EVENT_TYPES
from apipi.worker.pi.map import map_pi_event
from apipi.worker.pi.version import PINNED_PI


def test_pinned_pi() -> None:
    assert PINNED_PI == "0.85.1"


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
