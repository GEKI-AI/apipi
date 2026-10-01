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
    assert "agent.session.turn.thinking.summary.completed" not in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.thinking.summary.failed" not in PUBLIC_EVENT_TYPES
    assert "agent.session.title.updated" not in PUBLIC_EVENT_TYPES
    assert "agent.session.turn.thinking.started" not in LIVE_EVENT_TYPES
    assert LIVE_EVENT_TYPES <= PUBLIC_EVENT_TYPES
    assert "pi.internal" not in PUBLIC_EVENT_TYPES


async def test_pi_harness_sets_and_clears_turn_context() -> None:
    import uuid
    from typing import Any

    from apipi.worker.pi.harness import PiHarness

    calls: list[tuple[str, object, object]] = []

    class _Broker:
        def set_context(self, session_id: object, agent_id: object) -> None:
            calls.append(("context", session_id, agent_id))

        def set_turn(self, turn_id: object) -> None:
            calls.append(("turn", turn_id, None))

        def clear_turn(self) -> None:
            calls.append(("clear", None, None))

    class _Proc:
        broker = _Broker()

        async def prompt(self, _text: str, **_kwargs: object):  # type: ignore[no-untyped-def]
            yield {"type": "agent_settled", "success": True}
            return

    class _Pool:
        async def get(self, *args: object, **kwargs: object) -> Any:  # type: ignore[no-untyped-def]
            return _Proc()

        def touch(self, session_id: object) -> None:
            del session_id

    harness = PiHarness(_Pool())  # ty: ignore[invalid-argument-type]
    session_id = uuid.uuid4()
    [
        event
        async for event in harness.generate(
            "hi",
            session_id=session_id,
            turn_id="turn-1",
            agent_id="agent-1",
        )
    ]
    assert calls[0] == ("context", str(session_id), "agent-1")
    assert calls[1] == ("turn", "turn-1", None)
    assert calls[-1] == ("clear", None, None)
