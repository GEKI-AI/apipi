import uuid

from apipi.gateway.request_id import resolve_request_id, valid_request_id


def test_generates_when_missing() -> None:
    first = resolve_request_id(None, None)
    second = resolve_request_id(None, None)
    uuid.UUID(first)
    uuid.UUID(second)
    assert first != second
    assert valid_request_id(first)


def test_echoes_incoming() -> None:
    assert resolve_request_id(None, "echo-me") == "echo-me"


def test_honors_client_id() -> None:
    assert resolve_request_id("client-me", "echo-me") == "client-me"


def test_rejects_invalid_client_id() -> None:
    assert resolve_request_id("", "echo-me") == "echo-me"
    assert resolve_request_id("x" * 513, "echo-me") == "echo-me"
    assert resolve_request_id("café", None) != "café"
    assert valid_request_id("a" * 512)
    assert not valid_request_id("a" * 513)
