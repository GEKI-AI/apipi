import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import pytest

from apipi.worker.egress.dns import (
    RCODE_FORMERR,
    RCODE_NOTIMP,
    RCODE_NXDOMAIN,
    RCODE_SERVFAIL,
    DnsFilter,
    parse_question,
)


def query(name: str, ident: int = 0x1234, qtype: int = 1) -> bytes:
    labels = b"".join(
        len(part).to_bytes(1, "big") + part.encode() for part in name.split(".")
    )
    header = ident.to_bytes(2, "big") + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    return header + labels + b"\x00" + qtype.to_bytes(2, "big") + b"\x00\x01"


def answer_for(packet: bytes) -> bytes:
    question = parse_question(packet)
    record = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
    return (
        packet[:2]
        + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
        + packet[12 : question.end]
        + record
    )


def rcode(packet: bytes) -> int:
    return packet[3] & 0x0F


class FakeResolver:
    def __init__(self) -> None:
        self.udp: list[str] = []
        self.tcp: list[str] = []
        self.raw: list[bytes] = []
        self.port = 0
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        owner = self

        class Proto(asyncio.DatagramProtocol):
            def connection_made(self, transport: asyncio.BaseTransport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                owner.udp.append(parse_question(data).name)
                owner.raw.append(data)
                assert isinstance(self.transport, asyncio.DatagramTransport)
                self.transport.sendto(answer_for(data), addr)

        tcp = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = int(tcp.sockets[0].getsockname()[1])
        self._tcp = tcp
        transport, _ = await loop.create_datagram_endpoint(
            Proto, local_addr=("127.0.0.1", self.port)
        )
        self._udp = transport

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        size = int.from_bytes(await reader.readexactly(2), "big")
        data = await reader.readexactly(size)
        self.tcp.append(parse_question(data).name)
        self.raw.append(data)
        reply = answer_for(data)
        writer.write(len(reply).to_bytes(2, "big") + reply)
        await writer.drain()
        writer.close()

    def close(self) -> None:
        if self._udp is not None:
            self._udp.close()
        if self._tcp is not None:
            self._tcp.close()


@pytest.fixture
async def resolver() -> AsyncIterator[FakeResolver]:
    fake = FakeResolver()
    await fake.start()
    yield fake
    fake.close()


@pytest.fixture
async def dns(resolver: FakeResolver) -> AsyncIterator[DnsFilter]:
    allowed = {"api.example.com"}
    filt = DnsFilter(
        host="127.0.0.1",
        allow=lambda name: name in allowed,
        upstreams=(("127.0.0.1", resolver.port),),
        timeout=1.0,
    )
    await filt.start()
    yield filt
    await filt.stop()


async def udp_ask(port: int, packet: bytes) -> bytes:
    loop = asyncio.get_running_loop()
    got: asyncio.Future[bytes] = loop.create_future()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
            if not got.done():
                got.set_result(data)

    transport, _ = await loop.create_datagram_endpoint(
        Proto, remote_addr=("127.0.0.1", port)
    )
    try:
        transport.sendto(packet)
        return await asyncio.wait_for(got, timeout=3)
    finally:
        transport.close()


async def tcp_ask(port: int, packet: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(len(packet).to_bytes(2, "big") + packet)
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(2), "big")
        return await reader.readexactly(size)
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def answered(reply: bytes, packet: bytes) -> bool:
    return (
        reply[:2] == packet[:2]
        and rcode(reply) == 0
        and int.from_bytes(reply[6:8], "big") == 1
        and reply.endswith(b"\x5d\xb8\xd8\x22")
    )


async def test_forwarded_query_is_rebuilt(
    dns: DnsFilter, resolver: FakeResolver
) -> None:
    packet = bytearray(query("API.example.com", ident=0x0BAD))
    packet[3] |= 0x10
    packet[11] = 1
    packet += b"\x00\x00\x29\x10\x00\x00\x00\x00\x00\x00\x05guest"
    reply = await udp_ask(dns.udp_port, bytes(packet))
    assert answered(reply, bytes(packet))
    sent = resolver.raw[0]
    assert sent[2:4] == b"\x01\x00"
    assert sent[4:12] == b"\x00\x01\x00\x00\x00\x00\x00\x00"
    assert sent[12:] == query("api.example.com")[12:]
    assert b"guest" not in sent


async def test_https_records_get_no_data(
    dns: DnsFilter, resolver: FakeResolver
) -> None:
    for qtype in (64, 65):
        packet = query("api.example.com", qtype=qtype)
        reply = await udp_ask(dns.udp_port, packet)
        assert rcode(reply) == 0
        assert int.from_bytes(reply[6:8], "big") == 0
        assert parse_question(reply).qtype == qtype
    assert resolver.udp == []


async def test_private_names_get_the_placeholder(resolver: FakeResolver) -> None:
    allowed = {"api.example.com", "git.internal", "wiki.internal"}
    places = {"git.internal": "198.18.0.1", "wiki.internal": "198.18.0.2"}
    filt = DnsFilter(
        host="127.0.0.1",
        allow=lambda name: name in allowed,
        placeholder=lambda name: places.get(name),
        upstreams=(("127.0.0.1", resolver.port),),
        timeout=1.0,
    )
    await filt.start()
    try:
        packet = query("Git.Internal", ident=0x4242)
        for reply in (
            await udp_ask(filt.udp_port, packet),
            await tcp_ask(filt.tcp_port, packet),
        ):
            assert reply[:2] == packet[:2]
            assert rcode(reply) == 0
            assert int.from_bytes(reply[6:8], "big") == 1
            assert parse_question(reply).name == "git.internal"
            assert reply.endswith(socket.inet_aton("198.18.0.1"))
        for qtype in (28, 64, 65, 255):
            reply = await udp_ask(filt.udp_port, query("git.internal", qtype=qtype))
            assert rcode(reply) == 0
            assert int.from_bytes(reply[6:8], "big") == 0
        reply = await udp_ask(filt.udp_port, query("wiki.internal"))
        assert reply.endswith(socket.inet_aton("198.18.0.2"))
        reply = await udp_ask(filt.udp_port, query("other.internal"))
        assert rcode(reply) == RCODE_NXDOMAIN
        assert resolver.udp == [] and resolver.tcp == []
        reply = await udp_ask(filt.udp_port, query("api.example.com"))
        assert answered(reply, query("api.example.com"))
        assert resolver.udp == ["api.example.com"]
    finally:
        await filt.stop()


async def test_passthrough_answers_private_names_and_forwards_the_rest_unchanged(
    resolver: FakeResolver,
) -> None:
    filt = DnsFilter(
        host="127.0.0.1",
        allow=lambda _name: True,
        placeholder=lambda name: "198.18.0.1" if name == "git.internal" else None,
        passthrough=True,
        upstreams=(("127.0.0.1", resolver.port),),
        timeout=1.0,
    )
    await filt.start()
    try:
        private = query("Git.Internal", ident=0x4242)
        for reply in (
            await udp_ask(filt.udp_port, private),
            await tcp_ask(filt.tcp_port, private),
        ):
            assert reply[:2] == private[:2]
            assert reply.endswith(socket.inet_aton("198.18.0.1"))
        reply = await udp_ask(filt.udp_port, query("git.internal", qtype=28))
        assert rcode(reply) == 0
        assert int.from_bytes(reply[6:8], "big") == 0
        assert resolver.udp == [] and resolver.tcp == []
        packet = bytearray(query("ExAmple.COM", ident=0x0BAD))
        packet[3] |= 0x10
        packet[11] = 1
        packet += b"\x00\x00\x29\x10\x00\x00\x00\x00\x00\x00\x05guest"
        reply = await udp_ask(filt.udp_port, bytes(packet))
        assert answered(reply, bytes(packet))
        sent = resolver.raw[-1]
        assert sent[2:] == bytes(packet)[2:]
        reply = await tcp_ask(filt.tcp_port, bytes(packet))
        assert answered(reply, bytes(packet))
        assert resolver.raw[-1][2:] == bytes(packet)[2:]
        for qtype in (64, 65):
            reply = await udp_ask(filt.udp_port, query("other.internal", qtype=qtype))
            assert answered(reply, query("other.internal", qtype=qtype))
        chaos = bytearray(query("version.bind", qtype=16))
        chaos[-1] = 3
        reply = await udp_ask(filt.udp_port, bytes(chaos))
        assert reply[:2] == chaos[:2] and rcode(reply) == 0
        assert resolver.raw[-1][2:] == bytes(chaos)[2:]
        assert resolver.udp[-3:] == ["other.internal", "other.internal", "version.bind"]
    finally:
        await filt.stop()


def _edited(packet: bytes, at: int, value: int, extra: bytes = b"") -> bytes:
    changed = bytearray(packet)
    changed[at] = value
    return bytes(changed) + extra


ALLOWED = query("api.example.com")


@pytest.mark.parametrize(
    ("packet", "code"),
    [
        (_edited(ALLOWED, 5, 2, ALLOWED[12:]), RCODE_FORMERR),
        (_edited(ALLOWED, -1, 3), RCODE_NOTIMP),
        (ALLOWED[:15], RCODE_FORMERR),
        (_edited(ALLOWED, 2, 0x28), RCODE_NOTIMP),
        (_edited(ALLOWED, 2, 0x80), None),
    ],
    ids=["two_questions", "other_class", "truncated", "update_opcode", "response"],
)
async def test_bad_queries_are_refused(
    dns: DnsFilter, resolver: FakeResolver, packet: bytes, code: int | None
) -> None:
    if code is None:
        assert await dns.answer(packet, tcp=False) is None
    else:
        assert rcode(await udp_ask(dns.udp_port, packet)) == code
    assert resolver.udp == []


async def test_udp_queries_in_flight_are_capped(
    resolver: FakeResolver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.egress.dns.MAX_UDP_INFLIGHT", 0)
    filt = DnsFilter(
        host="127.0.0.1",
        allow=lambda name: True,
        upstreams=(("127.0.0.1", resolver.port),),
        timeout=1.0,
    )
    await filt.start()
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(udp_ask(filt.udp_port, query("a.example")), 0.3)
    finally:
        await filt.stop()
    assert resolver.udp == []


async def test_tcp_clients_are_capped(
    dns: DnsFilter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.egress.dns.MAX_TCP_CLIENTS", 1)
    reader, writer = await asyncio.open_connection("127.0.0.1", dns.tcp_port)
    await asyncio.sleep(0.05)
    with pytest.raises((asyncio.IncompleteReadError, ConnectionError)):
        await tcp_ask(dns.tcp_port, query("api.example.com"))
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    del reader


@pytest.mark.parametrize("tcp", [False, True], ids=["udp", "tcp"])
async def test_allowed_names_are_forwarded_and_others_get_nxdomain(
    dns: DnsFilter, resolver: FakeResolver, tcp: bool
) -> None:
    async def ask(packet: bytes) -> bytes:
        if tcp:
            return await tcp_ask(dns.tcp_port, packet)
        return await udp_ask(dns.udp_port, packet)

    packet = query("API.example.com", ident=7)
    assert answered(await ask(packet), packet)
    for name in ("evil.example.com", "example.com", "secret-data.attacker.test"):
        packet = query(name, ident=0x4321, qtype=16)
        reply = await ask(packet)
        assert reply[:2] == packet[:2]
        assert reply[2] & 0x80
        assert rcode(reply) == RCODE_NXDOMAIN
        assert parse_question(reply).name == name
        assert int.from_bytes(reply[6:8], "big") == 0
    assert (resolver.tcp if tcp else resolver.udp) == ["api.example.com"]
    assert (resolver.udp if tcp else resolver.tcp) == []


def test_reply_must_echo_the_question() -> None:
    from apipi.worker.egress.dns import reply_matches

    sent = query("api.example.com", ident=9)
    assert reply_matches(answer_for(sent), sent)
    assert not reply_matches(answer_for(query("evil.example.com", ident=9)), sent)
    assert not reply_matches(
        answer_for(query("api.example.com", ident=9, qtype=28)), sent
    )
    assert not reply_matches(answer_for(query("api.example.com", ident=10)), sent)
    assert not reply_matches(sent, sent)


class _Liar(asyncio.DatagramProtocol):
    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        assert isinstance(self.transport, asyncio.DatagramTransport)
        forged = query("evil.example.com", ident=int.from_bytes(data[:2], "big"))
        self.transport.sendto(answer_for(forged), addr)


@pytest.mark.parametrize("upstream", ["down", "mismatched_reply"])
async def test_upstream_without_a_matching_reply_is_servfail(upstream: str) -> None:
    liar: asyncio.DatagramTransport | None = None
    if upstream == "down":
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
    else:
        liar, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            _Liar, local_addr=("127.0.0.1", 0)
        )
        port = liar.get_extra_info("sockname")[1]
    filt = DnsFilter(
        host="127.0.0.1",
        allow=lambda name: True,
        upstreams=(("127.0.0.1", port),),
        timeout=0.2,
    )
    await filt.start()
    try:
        reply = await udp_ask(filt.udp_port, query("api.example.com"))
        assert rcode(reply) == RCODE_SERVFAIL
    finally:
        await filt.stop()
        if liar is not None:
            liar.close()
