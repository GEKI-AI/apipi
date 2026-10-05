import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import h11
import pytest

from apipi.common.metrics import Metrics
from apipi.worker.egress import (
    EgressGateway,
    EgressHooks,
    EgressMode,
    EgressPolicy,
    WorkerCA,
)
from apipi.worker.egress.dns import parse_question
from apipi.worker.egress.resolve import address_blocked

HOST = "allowed.test"


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


def answered(reply: bytes, packet: bytes) -> bool:
    return (
        reply[:2] == packet[:2]
        and rcode(reply) == 0
        and int.from_bytes(reply[6:8], "big") == 1
        and reply.endswith(b"\x5d\xb8\xd8\x22")
    )


def static_resolver(
    table: dict[str, list[str]],
) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, _port: int) -> list[str]:
        if host not in table:
            raise OSError("unknown host")
        return table[host]

    return resolve


def handmade_hello(server_name: str) -> bytes:
    name = server_name.encode()
    entry = b"\x00" + len(name).to_bytes(2, "big") + name
    sni = len(entry).to_bytes(2, "big") + entry
    extension = b"\x00\x00" + len(sni).to_bytes(2, "big") + sni
    body = (
        b"\x03\x03"
        + b"\x11" * 32
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + len(extension).to_bytes(2, "big")
        + extension
    )
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(handshake).to_bytes(2, "big") + handshake


@dataclass
class Seen:
    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes
    http_version: str


@dataclass
class Upstream:
    ca: WorkerCA
    tls: bool = True
    mode: str = "http"
    bind: str = "127.0.0.1"
    port: int = 0
    seen: list[Seen] = field(default_factory=list)
    connections: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> None:
        context = self.ca.server_context(HOST) if self.tls else None
        self._server = await asyncio.start_server(
            self._serve, self.bind, 0, ssl=context, limit=1 << 17
        )
        self.port = int(self._server.sockets[0].getsockname()[1])

    def close(self) -> None:
        if self._server is not None:
            self._server.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        try:
            if self.mode == "upgrade":
                head = await reader.readuntil(b"\r\n\r\n")
                self.seen.append(Seen("GET", "/ws", [], head, "1.1"))
                writer.write(
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\nready"
                )
            if self.mode in ("echo", "upgrade"):
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
                return
            await self._http(reader, writer)
        except (OSError, ConnectionError, h11.ProtocolError):
            return
        finally:
            writer.close()

    async def _http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = h11.Connection(h11.SERVER)
        while True:
            request: h11.Request | None = None
            body = bytearray()
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    conn.receive_data(await reader.read(65536))
                    continue
                if isinstance(event, h11.ConnectionClosed):
                    return
                if isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                elif isinstance(event, h11.EndOfMessage):
                    break
            assert request is not None
            target = request.target.decode()
            self.seen.append(
                Seen(
                    method=request.method.decode(),
                    target=target,
                    headers=[
                        (k.decode(), v.decode()) for k, v in request.headers.raw_items()
                    ],
                    body=bytes(body),
                    http_version=request.http_version.decode(),
                )
            )
            if target.startswith("/big/"):
                size = int(target.removeprefix("/big/"))
                writer.write(
                    conn.send(
                        h11.Response(
                            status_code=200,
                            headers=[("content-length", str(size))],
                        )
                    )
                )
                chunk = b"\0" * 65536
                left = size
                while left:
                    part = chunk[: min(left, len(chunk))]
                    writer.write(conn.send(h11.Data(data=part)))
                    left -= len(part)
                    await writer.drain()
                writer.write(conn.send(h11.EndOfMessage()))
            else:
                reply = f"hello {request.method.decode()} {target} {len(body)}".encode()
                writer.write(
                    conn.send(
                        h11.Response(
                            status_code=200,
                            headers=[
                                ("content-length", str(len(reply))),
                                ("x-upstream", "yes"),
                            ],
                        )
                    )
                )
                writer.write(conn.send(h11.Data(data=reply)))
                writer.write(conn.send(h11.EndOfMessage()))
            await writer.drain()
            if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                return
            conn.start_next_cycle()


def loopback_allowed_blocked(address: str) -> bool:
    return address != "127.0.0.1" and address_blocked(address)


@dataclass
class Env:
    tmp: Path
    worker_ca: WorkerCA
    upstream_ca: WorkerCA
    upstream_ca_file: Path
    gateways: list[EgressGateway] = field(default_factory=list)
    upstreams: list[Upstream] = field(default_factory=list)

    async def upstream(
        self, *, tls: bool = True, mode: str = "http", bind: str = "127.0.0.1"
    ) -> Upstream:
        server = Upstream(self.upstream_ca, tls=tls, mode=mode, bind=bind)
        await server.start()
        self.upstreams.append(server)
        return server

    async def gateway(
        self,
        mode: str,
        *,
        port: int,
        allowed: tuple[str, ...] = (HOST,),
        intercept: tuple[str, ...] = (),
        private: tuple[str, ...] = (),
        dest: tuple[str, int] | None = None,
        table: dict[str, list[str]] | None = None,
        http: bool = False,
        hooks: EgressHooks | None = None,
        metrics: Metrics | None = None,
        upstream_ca: bool = True,
        peek_timeout: float = 2.0,
        idle_timeout: float = 300.0,
        max_connections: int = 128,
        dns_upstreams: tuple[tuple[str, int], ...] = (),
    ) -> EgressGateway:
        policy = EgressPolicy.build(
            cast(EgressMode, mode),
            allowed_hosts=allowed,
            private_hosts=private,
            intercept_hosts=intercept,
        )
        target = dest if dest is not None else ("198.51.100.7", port)
        gateway = EgressGateway(
            host="127.0.0.1",
            policy=policy,
            ca=self.worker_ca,
            session_id="sess_egress",
            upstream_ca=str(self.upstream_ca_file) if upstream_ca else None,
            hooks=hooks,
            resolve=static_resolver(
                table if table is not None else {HOST: ["127.0.0.1"]}
            ),
            original_dst=lambda _sock: target,
            blocked=loopback_allowed_blocked,
            metrics=metrics,
            tls_ports=frozenset() if http else frozenset({port}),
            http_ports=frozenset({port}) if http else frozenset(),
            peek_timeout=peek_timeout,
            idle_timeout=idle_timeout,
            max_connections=max_connections,
            dns_upstreams=dns_upstreams,
        )
        await gateway.start()
        self.gateways.append(gateway)
        return gateway


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    upstream_ca = WorkerCA()
    path = tmp_path / "upstream-ca.pem"
    path.write_bytes(upstream_ca.cert_pem)
    state = Env(tmp_path, WorkerCA(), upstream_ca, path)
    yield state
    for gateway in state.gateways:
        await gateway.stop()
    for server in state.upstreams:
        server.close()


def trust(*cas: WorkerCA, alpn: list[str] | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(
        cadata="".join(ca.cert_pem.decode() for ca in cas)
    )
    if alpn:
        context.set_alpn_protocols(alpn)
    return context


async def tls_connect(
    gateway: EgressGateway, context: ssl.SSLContext, sni: str | None = HOST
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(
        asyncio.open_connection(
            "127.0.0.1",
            gateway.port,
            ssl=context,
            server_hostname=sni,
            limit=1 << 17,
        ),
        timeout=5,
    )


async def close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


class HttpClient:
    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.conn = h11.Connection(h11.CLIENT)

    async def request(
        self,
        method: str,
        target: str,
        *,
        host: str = HOST,
        headers: list[tuple[str, str]] | None = None,
        body: bytes = b"",
        chunked: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        if self.conn.our_state is h11.DONE:
            self.conn.start_next_cycle()
        items = [("host", host), *(headers or [])]
        if body and chunked:
            items.append(("transfer-encoding", "chunked"))
        elif body:
            items.append(("content-length", str(len(body))))
        self.writer.write(
            self.conn.send(h11.Request(method=method, target=target, headers=items))
        )
        if body:
            half = len(body) // 2
            for part in (body[:half], body[half:]):
                if part:
                    self.writer.write(self.conn.send(h11.Data(data=part)))
                    await self.writer.drain()
        self.writer.write(self.conn.send(h11.EndOfMessage()))
        await self.writer.drain()
        status = 0
        response_headers: dict[str, str] = {}
        data = bytearray()
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                self.conn.receive_data(
                    await asyncio.wait_for(self.reader.read(65536), timeout=5)
                )
                continue
            if isinstance(event, h11.Response):
                status = event.status_code
                response_headers = {
                    k.decode(): v.decode() for k, v in event.headers.raw_items()
                }
            elif isinstance(event, h11.Data):
                data.extend(event.data)
            elif isinstance(event, (h11.EndOfMessage, h11.ConnectionClosed)):
                return status, response_headers, bytes(data)
