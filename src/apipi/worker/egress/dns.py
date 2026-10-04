import asyncio
import contextlib
import socket
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from apipi.worker.egress.sockets import bind_socket, close_writer

DNS_TIMEOUT = 3.0
TCP_IDLE = 10.0
RCODE_FORMERR = 1
RCODE_SERVFAIL = 2
RCODE_NXDOMAIN = 3
RCODE_NOTIMP = 4

Upstream = tuple[str, int]


class DnsFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Question:
    name: str
    qtype: int
    end: int


def parse_question(packet: bytes) -> Question:
    if len(packet) < 12:
        raise DnsFormatError("short DNS header")
    if int.from_bytes(packet[4:6], "big") != 1:
        raise DnsFormatError("DNS query needs one question")
    labels: list[str] = []
    pos = 12
    while True:
        if pos >= len(packet):
            raise DnsFormatError("truncated DNS name")
        size = packet[pos]
        pos += 1
        if size == 0:
            break
        if size & 0xC0:
            raise DnsFormatError("compressed DNS question")
        label = packet[pos : pos + size]
        if len(label) != size:
            raise DnsFormatError("truncated DNS name")
        try:
            labels.append(label.decode("ascii").lower())
        except UnicodeDecodeError as exc:
            raise DnsFormatError("DNS name is not ASCII") from exc
        pos += size
    if pos + 4 > len(packet):
        raise DnsFormatError("truncated DNS question")
    name = ".".join(labels)
    if len(name) > 253:
        raise DnsFormatError("DNS name is too long")
    qtype = int.from_bytes(packet[pos : pos + 2], "big")
    return Question(name=name, qtype=qtype, end=pos + 4)


def error_reply(query: bytes, rcode: int, question_end: int | None = None) -> bytes:
    flags_in = int.from_bytes(query[2:4], "big") if len(query) >= 4 else 0
    flags = 0x8000 | (flags_in & 0x7800) | (flags_in & 0x0100) | 0x0080 | rcode
    ident = query[:2].ljust(2, b"\0")
    question = query[12:question_end] if question_end is not None else b""
    count = 1 if question else 0
    header = (
        ident
        + flags.to_bytes(2, "big")
        + count.to_bytes(2, "big")
        + (0).to_bytes(6, "big")
    )
    return header + question


class _UdpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "DnsFilter") -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.DatagramTransport)
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.owner.spawn(self._reply(data, addr))

    async def _reply(self, data: bytes, addr: tuple[str, int]) -> None:
        answer = await self.owner.answer(data, tcp=False)
        if answer is not None and self.transport is not None:
            self.transport.sendto(answer, addr)


class _UpstreamProtocol(asyncio.DatagramProtocol):
    def __init__(self, ident: bytes) -> None:
        self.ident = ident
        self.reply: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data[:2] == self.ident and not self.reply.done():
            self.reply.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)


class DnsFilter:
    def __init__(
        self,
        *,
        host: str,
        allow: Callable[[str], bool],
        upstreams: tuple[Upstream, ...],
        timeout: float = DNS_TIMEOUT,
        freebind: bool = False,
    ) -> None:
        self.host = host
        self.allow = allow
        self.upstreams = upstreams
        self.timeout = timeout
        self.freebind = freebind
        self.udp_port = 0
        self.tcp_port = 0
        self._udp: asyncio.DatagramTransport | None = None
        self._tcp: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        udp_sock = bind_socket(self.host, socket.SOCK_DGRAM, freebind=self.freebind)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProtocol(self), sock=udp_sock
        )
        self._udp = transport
        self.udp_port = int(udp_sock.getsockname()[1])
        tcp_sock = bind_socket(self.host, socket.SOCK_STREAM, freebind=self.freebind)
        try:
            self._tcp = await asyncio.start_server(self._serve_tcp, sock=tcp_sock)
        except OSError:
            tcp_sock.close()
            self.close()
            raise
        self.tcp_port = int(tcp_sock.getsockname()[1])

    def close(self) -> None:
        if self._udp is not None:
            self._udp.close()
            self._udp = None
        if self._tcp is not None:
            self._tcp.close()
            self._tcp.abort_clients()
            self._tcp = None
        for task in list(self._tasks):
            task.cancel()

    async def stop(self) -> None:
        tasks = list(self._tasks)
        self.close()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    async def answer(self, query: bytes, *, tcp: bool) -> bytes | None:
        if len(query) < 12 or query[2] & 0x80:
            return None
        if (query[2] >> 3) & 0x0F != 0:
            return error_reply(query, RCODE_NOTIMP)
        try:
            question = parse_question(query)
        except DnsFormatError:
            return error_reply(query, RCODE_FORMERR)
        if not question.name or not self.allow(question.name):
            return error_reply(query, RCODE_NXDOMAIN, question.end)
        for upstream in self.upstreams:
            try:
                if tcp:
                    return await self._forward_tcp(query, upstream)
                return await self._forward_udp(query, upstream)
            except (OSError, TimeoutError, asyncio.IncompleteReadError):
                continue
        return error_reply(query, RCODE_SERVFAIL, question.end)

    async def _forward_udp(self, query: bytes, upstream: Upstream) -> bytes:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _UpstreamProtocol(query[:2]), remote_addr=upstream
        )
        try:
            transport.sendto(query)
            return await asyncio.wait_for(protocol.reply, timeout=self.timeout)
        finally:
            transport.close()

    async def _forward_tcp(self, query: bytes, upstream: Upstream) -> bytes:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(*upstream), timeout=self.timeout
        )
        try:
            writer.write(len(query).to_bytes(2, "big") + query)
            await writer.drain()
            size = int.from_bytes(
                await asyncio.wait_for(reader.readexactly(2), timeout=self.timeout),
                "big",
            )
            return await asyncio.wait_for(
                reader.readexactly(size), timeout=self.timeout
            )
        finally:
            await close_writer(writer)

    async def _serve_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                head = await asyncio.wait_for(reader.readexactly(2), timeout=TCP_IDLE)
                size = int.from_bytes(head, "big")
                query = await asyncio.wait_for(
                    reader.readexactly(size), timeout=TCP_IDLE
                )
                reply = await self.answer(query, tcp=True)
                if reply is None:
                    return
                writer.write(len(reply).to_bytes(2, "big") + reply)
                await writer.drain()
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            return
        finally:
            await close_writer(writer)
