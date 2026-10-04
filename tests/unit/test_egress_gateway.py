import asyncio
import contextlib
import logging
import socket
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import h11
import pytest
from tests.unit.test_egress_sni import handmade_hello

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.worker.egress import (
    EgressGateway,
    EgressHooks,
    EgressMode,
    EgressPolicy,
    Reject,
    RequestHead,
    ResponseHead,
    WorkerCA,
    start_gateway,
)

HOST = "allowed.test"


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
    port: int = 0
    seen: list[Seen] = field(default_factory=list)
    connections: int = 0
    _server: asyncio.Server | None = None

    async def start(self) -> None:
        context = self.ca.server_context(HOST) if self.tls else None
        self._server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, ssl=context, limit=1 << 17
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
                    b"Upgrade: echo\r\nConnection: Upgrade\r\n\r\nready"
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


def resolver(table: dict[str, list[str]]) -> Callable[[str, int], Awaitable[list[str]]]:
    async def resolve(host: str, _port: int) -> list[str]:
        if host not in table:
            raise OSError("unknown host")
        return table[host]

    return resolve


@dataclass
class Env:
    tmp: Path
    worker_ca: WorkerCA
    upstream_ca: WorkerCA
    upstream_ca_file: Path
    gateways: list[EgressGateway] = field(default_factory=list)
    upstreams: list[Upstream] = field(default_factory=list)

    async def upstream(self, *, tls: bool = True, mode: str = "http") -> Upstream:
        server = Upstream(self.upstream_ca, tls=tls, mode=mode)
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
        private: tuple[str, ...] = ("127.0.0.1/32",),
        dest: tuple[str, int] | None = None,
        table: dict[str, list[str]] | None = None,
        http: bool = False,
        hooks: EgressHooks | None = None,
        metrics: Metrics | None = None,
        upstream_ca: bool = True,
        peek_timeout: float = 2.0,
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
            resolve=resolver(table if table is not None else {HOST: ["127.0.0.1"]}),
            original_dst=lambda _sock: target,
            metrics=metrics,
            tls_ports=frozenset() if http else frozenset({port}),
            http_ports=frozenset({port}) if http else frozenset(),
            peek_timeout=peek_timeout,
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


async def wait_record(
    caplog: pytest.LogCaptureFixture, **fields: Any
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for record in caplog.records:
            if getattr(record, "event", None) != "egress.connection":
                continue
            if all(
                getattr(record, key, None) == value for key, value in fields.items()
            ):
                return dict(record.__dict__)
        await asyncio.sleep(0.01)
    raise AssertionError(f"no egress.connection record with {fields}")


async def assert_rejected(
    gateway: EgressGateway, context: ssl.SSLContext, sni: str | None = HOST
) -> None:
    with pytest.raises((OSError, ssl.SSLError, asyncio.IncompleteReadError)):
        reader, writer = await tls_connect(gateway, context, sni)
        try:
            writer.write(b"ping")
            await writer.drain()
            if not await asyncio.wait_for(reader.read(1), timeout=5):
                raise ConnectionResetError("closed")
        finally:
            await close(writer)


async def test_splice_keeps_end_to_end_tls(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    metrics = Metrics()
    gateway = await env.gateway("restricted", port=upstream.port, metrics=metrics)
    reader, writer = await tls_connect(gateway, trust(env.upstream_ca))
    cert = writer.get_extra_info("peercert")
    assert ("commonName", HOST) in cert["subject"][0]
    writer.write(b"pinned client")
    await writer.drain()
    assert await reader.readexactly(13) == b"pinned client"
    await close(writer)
    record = await wait_record(caplog, decision="spliced")
    assert record["host"] == HOST
    assert record["port"] == upstream.port
    assert record["session_id"] == "sess_egress"
    assert record["bytes_up"] > 13
    assert record["bytes_down"] > 13
    assert "reason" not in record
    sample = metrics.registry.get_sample_value
    assert sample("apipi_egress_connections_total", {"decision": "spliced"}) == 1
    assert sample("apipi_egress_bytes_total", {"direction": "up"}) == record["bytes_up"]
    assert (
        sample("apipi_egress_bytes_total", {"direction": "down"})
        == record["bytes_down"]
    )


async def test_restricted_rejects_other_hosts_ip_literals_and_no_sni(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    metrics = Metrics()
    gateway = await env.gateway(
        "restricted",
        port=upstream.port,
        table={HOST: ["127.0.0.1"], "other.test": ["127.0.0.1"]},
        metrics=metrics,
    )
    await assert_rejected(gateway, trust(env.upstream_ca), "other.test")
    await wait_record(caplog, decision="rejected", reason="not_allowed")
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    await assert_rejected(gateway, loose, None)
    await wait_record(caplog, decision="rejected", reason="no_host")
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(handmade_hello("1.1.1.1"))
    await writer.drain()
    with contextlib.suppress(ConnectionError):
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    await close(writer)
    await wait_record(caplog, decision="rejected", reason="ip_literal")
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(b"SSH-2.0-OpenSSH_9.6\r\n")
    await writer.drain()
    with contextlib.suppress(ConnectionError):
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    await close(writer)
    await wait_record(caplog, decision="rejected", reason="not_tls")
    assert upstream.connections == 0
    assert (
        metrics.registry.get_sample_value(
            "apipi_egress_connections_total", {"decision": "rejected"}
        )
        == 4
    )


async def test_gateway_connects_to_its_own_resolution(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    gateway = await env.gateway(
        "restricted", port=upstream.port, dest=("203.0.113.66", upstream.port)
    )
    reader, writer = await tls_connect(gateway, trust(env.upstream_ca))
    writer.write(b"x")
    await writer.drain()
    assert await reader.readexactly(1) == b"x"
    await close(writer)
    assert upstream.connections == 1


async def test_private_address_rejected_in_every_mode(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    for mode in ("enabled", "restricted"):
        gateway = await env.gateway(mode, port=upstream.port, private=())
        await assert_rejected(gateway, trust(env.upstream_ca))
        await wait_record(caplog, decision="rejected", reason="private_address")
        caplog.clear()
    disabled = await env.gateway("disabled", port=upstream.port)
    await assert_rejected(disabled, trust(env.upstream_ca))
    await wait_record(caplog, decision="rejected", reason="disabled")
    assert upstream.connections == 0


async def test_private_hosts_allow_named_upstream(env: Env) -> None:
    upstream = await env.upstream(mode="echo")
    gateway = await env.gateway("restricted", port=upstream.port, private=(HOST,))
    reader, writer = await tls_connect(gateway, trust(env.upstream_ca))
    writer.write(b"forgejo")
    await writer.drain()
    assert await reader.readexactly(7) == b"forgejo"
    await close(writer)
    other = await env.gateway(
        "restricted", port=upstream.port, private=("forgejo.internal",)
    )
    await assert_rejected(other, trust(env.upstream_ca))


async def test_enabled_splices_ip_literal_and_no_sni_to_original_destination(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    gateway = await env.gateway(
        "enabled", port=upstream.port, dest=("127.0.0.1", upstream.port)
    )
    loose = ssl.create_default_context()
    loose.check_hostname = False
    loose.verify_mode = ssl.CERT_NONE
    reader, writer = await tls_connect(gateway, loose, None)
    writer.write(b"raw")
    await writer.drain()
    assert await reader.readexactly(3) == b"raw"
    await close(writer)
    record = await wait_record(caplog, decision="spliced")
    assert "host" not in record


async def test_other_ports_rejected(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(mode="echo")
    gateway = await env.gateway("enabled", port=upstream.port, dest=("127.0.0.1", 22))
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    with contextlib.suppress(ConnectionError):
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    await close(writer)
    await wait_record(caplog, decision="rejected", reason="port", port=22)


async def test_plain_http_checks_host_header(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream(tls=False)
    gateway = await env.gateway(
        "restricted",
        port=upstream.port,
        http=True,
        table={HOST: ["127.0.0.1"], "other.test": ["127.0.0.1"]},
    )
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    client = HttpClient(reader, writer)
    status, _, body = await client.request("GET", "/plain")
    assert status == 200
    assert body == b"hello GET /plain 0"
    await close(writer)
    await wait_record(caplog, decision="spliced", host=HOST)
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    status, _, body = await HttpClient(reader, writer).request(
        "GET", "/", host="other.test"
    )
    assert status == 403
    await close(writer)
    await wait_record(caplog, decision="rejected", reason="not_allowed")
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    status, _, _ = await HttpClient(reader, writer).request("GET", "/", host="1.1.1.1")
    assert status == 403
    await close(writer)
    await wait_record(caplog, decision="rejected", reason="ip_literal")
    assert len(upstream.seen) == 1


async def test_intercept_terminates_tls_and_forwards_http11(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream()
    gateway = await env.gateway("restricted", port=upstream.port, intercept=(HOST,))
    reader, writer = await tls_connect(
        gateway, trust(env.worker_ca, alpn=["h2", "http/1.1"])
    )
    ssl_object = writer.get_extra_info("ssl_object")
    assert ssl_object.selected_alpn_protocol() == "http/1.1"
    issuer = dict(item[0] for item in ssl_object.getpeercert()["issuer"])
    assert issuer["commonName"] == "ApiPi worker egress CA"
    client = HttpClient(reader, writer)
    status, headers, body = await client.request(
        "GET", "/v1/items?q=1", headers=[("X-Custom", "abc")]
    )
    assert status == 200
    assert headers["x-upstream"] == "yes"
    assert body == b"hello GET /v1/items?q=1 0"
    payload = b"z" * 300_000
    status, _, body = await client.request("POST", "/upload", body=payload)
    assert body == b"hello POST /upload 300000"
    status, _, body = await client.request(
        "PUT", "/chunked", body=b"abc" * 1000, chunked=True
    )
    assert body == b"hello PUT /chunked 3000"
    await close(writer)
    first, second, third = upstream.seen
    assert first.method == "GET"
    assert first.target == "/v1/items?q=1"
    assert first.http_version == "1.1"
    assert ("host", HOST) in first.headers
    assert ("X-Custom", "abc") in first.headers
    assert second.body == payload
    assert third.body == b"abc" * 1000
    assert upstream.connections == 1
    record = await wait_record(caplog, decision="intercepted")
    assert record["host"] == HOST
    assert record["bytes_up"] > len(payload)
    for item in caplog.records:
        assert "abc" not in item.getMessage()
        assert "abc" not in str(item.__dict__.values())


async def test_intercept_passes_protocol_upgrade(env: Env) -> None:
    upstream = await env.upstream(mode="upgrade")
    gateway = await env.gateway("restricted", port=upstream.port, intercept=(HOST,))
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    writer.write(
        f"GET /ws HTTP/1.1\r\nHost: {HOST}\r\nUpgrade: echo\r\n"
        "Connection: Upgrade\r\n\r\n".encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    assert head.startswith(b"HTTP/1.1 101")
    assert await asyncio.wait_for(reader.readexactly(5), timeout=5) == b"ready"
    writer.write(b"frame")
    await writer.drain()
    assert await asyncio.wait_for(reader.readexactly(5), timeout=5) == b"frame"
    await close(writer)
    assert b"upgrade: echo" in upstream.seen[0].body.lower()


async def test_intercept_requires_host_to_match_sni(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream()
    gateway = await env.gateway("restricted", port=upstream.port, intercept=(HOST,))
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    status, _, _ = await HttpClient(reader, writer).request(
        "GET", "/", host="evil.test"
    )
    assert status == 421
    await close(writer)
    await wait_record(caplog, decision="intercepted", reason="host_mismatch")
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    status, _, _ = await HttpClient(reader, writer).request(
        "GET", "/", host=f"{HOST}:{upstream.port}"
    )
    assert status == 200
    await close(writer)
    assert len(upstream.seen) == 1


async def test_hooks_modify_and_reject(env: Env) -> None:
    upstream = await env.upstream()
    hooks = EgressHooks()
    gateway = await env.gateway(
        "enabled", port=upstream.port, intercept=(HOST,), hooks=hooks
    )

    def add_auth(head: RequestHead) -> RequestHead | Reject | None:
        if head.target == "/deny":
            return Reject(status=403, reason="hook_denied")
        if head.header("x-placeholder") is None:
            return None
        headers = tuple(
            (name, "Bearer real") if name.lower() == "x-placeholder" else (name, value)
            for name, value in head.headers
        )
        return replace(head, headers=headers)

    async def mask(head: RequestHead, response: ResponseHead) -> ResponseHead:
        return replace(response, headers=(*response.headers, ("x-masked", "1")))

    gateway.add_request_hook(add_auth)
    gateway.add_response_hook(mask)
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    client = HttpClient(reader, writer)
    status, headers, _ = await client.request(
        "GET", "/ok", headers=[("X-Placeholder", "PLACEHOLDER")]
    )
    assert status == 200
    assert headers["x-masked"] == "1"
    status, _, _ = await client.request("GET", "/deny")
    assert status == 403
    await close(writer)
    assert len(upstream.seen) == 1
    assert ("X-Placeholder", "Bearer real") in upstream.seen[0].headers


async def test_intercept_set_can_change_before_connect(env: Env) -> None:
    upstream = await env.upstream()
    gateway = await env.gateway("restricted", port=upstream.port)
    gateway.set_intercept_hosts([HOST])
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    status, _, _ = await HttpClient(reader, writer).request("GET", "/")
    assert status == 200
    await close(writer)
    gateway.set_intercept_hosts([])
    reader, writer = await tls_connect(gateway, trust(env.upstream_ca))
    status, _, _ = await HttpClient(reader, writer).request("GET", "/")
    assert status == 200
    await close(writer)


async def test_intercept_verifies_upstream_certificate(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    upstream = await env.upstream()
    gateway = await env.gateway(
        "restricted", port=upstream.port, intercept=(HOST,), upstream_ca=False
    )
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    status, _, _ = await HttpClient(reader, writer).request("GET", "/")
    assert status == 502
    await close(writer)
    await wait_record(caplog, decision="intercepted", reason="upstream_tls")
    assert upstream.seen == []


async def test_client_without_worker_ca_fails_closed(env: Env) -> None:
    upstream = await env.upstream()
    gateway = await env.gateway("restricted", port=upstream.port, intercept=(HOST,))
    with pytest.raises(ssl.SSLCertVerificationError):
        await tls_connect(gateway, trust(env.upstream_ca))
    assert upstream.seen == []


async def test_start_gateway_reads_settings(tmp_path: Path) -> None:
    upstream_ca = WorkerCA()
    path = tmp_path / "ca.pem"
    path.write_bytes(upstream_ca.cert_pem)
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        microvm_egress_private_hosts="git.internal, 10.0.0.0/8",
        microvm_egress_upstream_ca=str(path),
    )
    gateway = await start_gateway(
        settings,
        host="127.0.0.1",
        mode="restricted",
        allowed_hosts=("Git.Internal",),
        session_id="sess_1",
        intercept_hosts=("git.internal",),
        dns_upstreams=(("127.0.0.1", 53),),
        freebind=False,
    )
    try:
        assert gateway.port > 0
        assert gateway.dns_ports is not None
        assert gateway.policy.allowed_hosts == frozenset({"git.internal"})
        assert gateway.policy.private_hosts == ("git.internal", "10.0.0.0/8")
        assert gateway.policy.intercept_hosts == frozenset({"git.internal"})
        assert b"BEGIN CERTIFICATE" in gateway.ca_pem
    finally:
        await gateway.stop()
    enabled = await start_gateway(
        settings, host="127.0.0.1", mode="enabled", freebind=False
    )
    try:
        assert enabled.dns_ports is None
    finally:
        await enabled.stop()


async def test_close_cancels_open_connections(env: Env) -> None:
    upstream = await env.upstream(mode="echo")
    gateway = await env.gateway("restricted", port=upstream.port)
    reader, writer = await tls_connect(gateway, trust(env.upstream_ca))
    writer.write(b"a")
    await writer.drain()
    assert await reader.readexactly(1) == b"a"
    gateway.close()
    assert await asyncio.wait_for(reader.read(), timeout=5) == b""
    await close(writer)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", gateway.port), timeout=1).close()


async def _throughput(reader: asyncio.StreamReader, total: int) -> tuple[float, float]:
    started = time.perf_counter()
    cpu = time.process_time()
    left = total
    while left:
        data = await reader.read(min(left, 1 << 17))
        assert data
        left -= len(data)
    return time.perf_counter() - started, time.process_time() - cpu


@pytest.mark.slow
async def test_throughput_spliced_and_intercepted(env: Env) -> None:
    total = 64 * 1024 * 1024
    upstream = await env.upstream()
    spliced = await env.gateway("restricted", port=upstream.port)
    reader, writer = await tls_connect(spliced, trust(env.upstream_ca))
    writer.write(f"GET /big/{total} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    splice_wall, splice_cpu = await _throughput(reader, total)
    await close(writer)
    intercepted = await env.gateway("restricted", port=upstream.port, intercept=(HOST,))
    reader, writer = await tls_connect(intercepted, trust(env.worker_ca))
    writer.write(f"GET /big/{total} HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    intercept_wall, intercept_cpu = await _throughput(reader, total)
    await close(writer)
    mbit = total * 8 / 1_000_000
    splice_rate = mbit / splice_wall
    intercept_rate = mbit / intercept_wall
    print(
        f"\negress throughput over {total // (1024 * 1024)} MiB: "
        f"spliced {splice_rate:.0f} Mbit/s ({splice_cpu:.2f} s CPU), "
        f"intercepted {intercept_rate:.0f} Mbit/s ({intercept_cpu:.2f} s CPU); "
        "CPU includes the test client and upstream in the same process"
    )
    assert splice_rate >= 50
    assert intercept_rate >= 50
