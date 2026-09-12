from apipi.runtime import PUBLIC_EVENT_TYPES, FakeHarness


def test_fake_harness_is_determined() -> None:
    harness = FakeHarness()
    assert harness.complete("hello") == "hello"
    assert harness.complete("") == "ok"
    assert harness.complete("hello") == "hello"


def test_public_event_types_match_spec() -> None:
    assert "agent.session.created" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.cancelled" in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.output_text.delta" in PUBLIC_EVENT_TYPES
    assert "pi.internal" not in PUBLIC_EVENT_TYPES
