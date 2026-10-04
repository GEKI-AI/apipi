import ipaddress
from dataclasses import dataclass

MAX_HELLO_BYTES = 65536
_RECORD_HANDSHAKE = 0x16
_HANDSHAKE_CLIENT_HELLO = 0x01
_EXT_SERVER_NAME = 0x0000
_EXT_ALPN = 0x0010
_NAME_HOST = 0x00


class NotTls(ValueError):
    pass


class Incomplete(Exception):
    pass


@dataclass(frozen=True)
class ClientHello:
    server_name: str | None
    alpn: tuple[str, ...] = ()


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def take(self, size: int) -> bytes:
        end = self.pos + size
        if end > len(self.data):
            raise NotTls("truncated ClientHello")
        chunk = self.data[self.pos : end]
        self.pos = end
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return int.from_bytes(self.take(2), "big")

    def u24(self) -> int:
        return int.from_bytes(self.take(3), "big")

    def vector(self, length_bytes: int) -> bytes:
        size = int.from_bytes(self.take(length_bytes), "big")
        return self.take(size)

    @property
    def done(self) -> bool:
        return self.pos >= len(self.data)


def _handshake_bytes(data: bytes) -> bytes:
    if not data:
        raise Incomplete
    if data[0] != _RECORD_HANDSHAKE:
        raise NotTls("not a TLS handshake record")
    body = bytearray()
    pos = 0
    need: int | None = None
    while need is None or len(body) < need:
        if len(data) < pos + 5:
            raise Incomplete
        if data[pos] != _RECORD_HANDSHAKE or data[pos + 1] != 0x03:
            raise NotTls("not a TLS handshake record")
        size = int.from_bytes(data[pos + 3 : pos + 5], "big")
        if size == 0:
            raise NotTls("empty TLS record")
        if len(data) < pos + 5 + size:
            raise Incomplete
        body.extend(data[pos + 5 : pos + 5 + size])
        pos += 5 + size
        if need is None and len(body) >= 4:
            if body[0] != _HANDSHAKE_CLIENT_HELLO:
                raise NotTls("first handshake message is not a ClientHello")
            need = 4 + int.from_bytes(body[1:4], "big")
            if need > MAX_HELLO_BYTES:
                raise NotTls("ClientHello is too large")
    return bytes(body[:need])


def _server_name(raw: bytes) -> str | None:
    names = _Reader(_Reader(raw).vector(2))
    while not names.done:
        kind = names.u8()
        value = names.vector(2)
        if kind != _NAME_HOST:
            continue
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise NotTls("server name is not ASCII") from exc
        host = text.rstrip(".").lower()
        return host or None
    return None


def _alpn(raw: bytes) -> tuple[str, ...]:
    protocols = _Reader(_Reader(raw).vector(2))
    found: list[str] = []
    while not protocols.done:
        found.append(protocols.vector(1).decode("ascii", "replace"))
    return tuple(found)


def parse_client_hello(data: bytes) -> ClientHello:
    hello = _Reader(_handshake_bytes(data))
    hello.u8()
    hello.u24()
    hello.take(2)
    hello.take(32)
    hello.vector(1)
    hello.vector(2)
    hello.vector(1)
    if hello.done:
        return ClientHello(server_name=None)
    extensions = _Reader(hello.vector(2))
    server_name: str | None = None
    alpn: tuple[str, ...] = ()
    while not extensions.done:
        kind = extensions.u16()
        value = extensions.vector(2)
        if kind == _EXT_SERVER_NAME:
            server_name = _server_name(value)
        elif kind == _EXT_ALPN:
            alpn = _alpn(value)
    return ClientHello(server_name=server_name, alpn=alpn)


def is_ip_literal(host: str) -> bool:
    text = host.strip("[]")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True
