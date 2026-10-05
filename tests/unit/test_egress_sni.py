import ssl

import pytest
from tests.support.egress import handmade_hello

from apipi.worker.egress.sni import (
    Incomplete,
    NotTls,
    is_ip_literal,
    parse_client_hello,
)


def client_hello(server_hostname: str | None, alpn: list[str] | None = None) -> bytes:
    context = ssl.create_default_context()
    if server_hostname is None:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if alpn:
        context.set_alpn_protocols(alpn)
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


def test_parse_sni_and_alpn_from_real_client_hello() -> None:
    hello = parse_client_hello(client_hello("API.Example.com", alpn=["h2", "http/1.1"]))
    assert hello.server_name == "api.example.com"
    assert hello.alpn == ("h2", "http/1.1")


def test_parse_without_sni() -> None:
    hello = parse_client_hello(client_hello(None))
    assert hello.server_name is None


def test_parse_ip_literal_server_name() -> None:
    hello = parse_client_hello(handmade_hello("1.1.1.1"))
    assert hello.server_name == "1.1.1.1"
    assert is_ip_literal("1.1.1.1")
    assert is_ip_literal("[2001:db8::1]")
    assert not is_ip_literal("api.example.com")


def test_partial_hello_needs_more_bytes() -> None:
    data = client_hello("api.example.com")
    for cut in (0, 3, 5, 20, len(data) - 1):
        with pytest.raises(Incomplete):
            parse_client_hello(data[:cut])
    assert parse_client_hello(data + b"trailing").server_name == "api.example.com"


def test_hello_split_over_two_records() -> None:
    data = client_hello("api.example.com")
    handshake = data[5:]
    first, second = handshake[:40], handshake[40:]
    records = (
        b"\x16\x03\x01"
        + len(first).to_bytes(2, "big")
        + first
        + b"\x16\x03\x01"
        + len(second).to_bytes(2, "big")
        + second
    )
    assert parse_client_hello(records).server_name == "api.example.com"
    with pytest.raises(Incomplete):
        parse_client_hello(records[: 5 + len(first) + 3])


def test_not_tls() -> None:
    with pytest.raises(NotTls):
        parse_client_hello(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    with pytest.raises(NotTls):
        parse_client_hello(b"SSH-2.0-OpenSSH_9.6\r\n")
    server_hello = b"\x16\x03\x03\x00\x04\x02\x00\x00\x00"
    with pytest.raises(NotTls):
        parse_client_hello(server_hello)


def hello_with_extensions(extensions: bytes) -> bytes:
    body = (
        b"\x03\x03"
        + b"\x11" * 32
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + len(extensions).to_bytes(2, "big")
        + extensions
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


def sni_extension(*names: str) -> bytes:
    entries = b"".join(
        b"\x00" + len(name).to_bytes(2, "big") + name.encode() for name in names
    )
    sni = len(entries).to_bytes(2, "big") + entries
    return b"\x00\x00" + len(sni).to_bytes(2, "big") + sni


def test_one_server_name_only() -> None:
    hello = hello_with_extensions(sni_extension("allowed.test"))
    assert parse_client_hello(hello).server_name == "allowed.test"
    with pytest.raises(NotTls, match="duplicate"):
        parse_client_hello(
            hello_with_extensions(
                sni_extension("allowed.test") + sni_extension("other.test")
            )
        )
    with pytest.raises(NotTls, match="more than one"):
        parse_client_hello(
            hello_with_extensions(sni_extension("allowed.test", "other.test"))
        )
