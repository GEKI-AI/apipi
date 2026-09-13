import pytest

from apipi.api.sessions import SSE_PING, _sse


def test_ping_is_comment_without_dispatch_blank_line() -> None:
    assert SSE_PING.startswith(":")
    assert not SSE_PING.endswith("\n\n")


def test_openai_decoder_does_not_emit_empty_data_on_ping() -> None:
    openai = pytest.importorskip("openai")
    decoder_cls = getattr(getattr(openai, "_streaming", None), "SSEDecoder", None)
    if decoder_cls is None:
        pytest.skip("openai.SSEDecoder missing")
    event = {
        "id": "e1",
        "type": "agent.session.idle",
        "seq": 1,
        "session_id": "s1",
        "created_at": "2026-09-13T00:00:00+00:00",
        "data": {},
    }
    blob = (_sse(event) + SSE_PING).encode()
    decoder = decoder_cls()
    events = list(decoder.iter_bytes(iter([blob])))
    assert len(events) == 1
    assert events[0].json()["type"] == "agent.session.idle"
