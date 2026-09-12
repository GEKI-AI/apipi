from apipi.pi.map import map_pi_event
from apipi.pi.version import PINNED_PI
from apipi.runtime import PUBLIC_EVENT_TYPES


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


def test_internal_pi_events_are_dropped() -> None:
    assert map_pi_event({"type": "agent_start"}) == []
    assert map_pi_event({"type": "turn_start"}) == []
    assert map_pi_event({"type": "extension_error", "error": "x"}) == []


def test_mapped_payload_has_no_pi_keys() -> None:
    mapped = map_pi_event(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
        }
    )
    payload = mapped[0][1]
    assert "assistantMessageEvent" not in payload
    assert "type" not in payload
