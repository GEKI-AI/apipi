from apipi.services.runtime import (
    FAKE_USAGE,
    LIVE_EVENT_TYPES,
    PUBLIC_EVENT_TYPES,
    FakeHarness,
)


def test_fake_harness_is_determined() -> None:
    harness = FakeHarness()
    assert harness.complete("hello") == "hello"
    assert harness.complete("") == "ok"
    assert harness.complete("hello") == "hello"


async def test_fake_harness_emits_usage() -> None:
    harness = FakeHarness()
    events = [event async for event in harness.generate("hello")]
    assert events[-1] == ("usage", FAKE_USAGE)
    assert "prompt" not in events[-1][1]


def test_public_event_types_match_spec() -> None:
    assert "agent.session.created" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.cancelled" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.output_text.delta" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.thinking.started" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.thinking.completed" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.thinking.started" not in LIVE_EVENT_TYPES
    assert LIVE_EVENT_TYPES <= PUBLIC_EVENT_TYPES
    assert "pi.internal" not in PUBLIC_EVENT_TYPES
