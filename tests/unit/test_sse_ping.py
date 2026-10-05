from apipi.api.sessions import SSE_PING


def test_ping_is_comment_without_dispatch_blank_line() -> None:
    assert SSE_PING.startswith(":")
    assert not SSE_PING.endswith("\n\n")
