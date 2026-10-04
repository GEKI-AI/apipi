import asyncio
import contextlib
import inspect
import socket
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import h11

from apipi.worker.egress.ca import INTERCEPT_ALPN
from apipi.worker.egress.policy import norm_host, split_host_port
from apipi.worker.egress.sockets import (
    CHUNK,
    UpstreamError,
    accept_stream,
    close_writer,
    open_upstream,
)
from apipi.worker.egress.splice import ByteCount, splice

HANDSHAKE_TIMEOUT = 10.0
MAX_HEAD_BYTES = 65536

Headers = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RequestHead:
    method: str
    target: str
    headers: Headers
    host: str
    port: int

    def header(self, name: str) -> str | None:
        key = name.lower()
        for item, value in self.headers:
            if item.lower() == key:
                return value
        return None


@dataclass(frozen=True)
class ResponseHead:
    status: int
    reason: str
    headers: Headers


@dataclass(frozen=True)
class Reject:
    status: int = 403
    reason: str = "rejected"


RequestResult = RequestHead | Reject | None
ResponseResult = ResponseHead | None
RequestHook = Callable[[RequestHead], RequestResult | Awaitable[RequestResult]]
ResponseHook = Callable[
    [RequestHead, ResponseHead], ResponseResult | Awaitable[ResponseResult]
]


@dataclass
class EgressHooks:
    request: list[RequestHook] = field(default_factory=list)
    response: list[ResponseHook] = field(default_factory=list)


class InterceptError(Exception):
    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def upstream_context(upstream_ca: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if upstream_ca:
        context.load_verify_locations(cafile=upstream_ca)
    context.set_alpn_protocols(list(INTERCEPT_ALPN))
    return context


def _text(raw: bytes) -> str:
    return raw.decode("latin-1")


def _raw(text: str) -> bytes:
    return text.encode("latin-1")


def _headers(items: Any) -> Headers:
    return tuple((_text(name), _text(value)) for name, value in items)


def _wire(headers: Headers) -> list[tuple[bytes, bytes]]:
    return [(_raw(name), _raw(value)) for name, value in headers]


async def _resolved(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def host_matches(head: RequestHead) -> bool:
    value = head.header("host")
    if value is None:
        return False
    try:
        name, port = split_host_port(value)
    except ValueError:
        return False
    if norm_host(name) != norm_host(head.host):
        return False
    return port is None or port == head.port


class _Side:
    def __init__(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        count: ByteCount | None = None,
    ) -> None:
        self.conn = conn
        self.reader = reader
        self.writer = writer
        self.count = count

    async def next_event(self) -> Any:
        while True:
            event = self.conn.next_event()
            if event is not h11.NEED_DATA:
                return event
            data = await self.reader.read(CHUNK)
            if self.count is not None:
                self.count.up += len(data)
            self.conn.receive_data(data)

    async def send(self, event: Any) -> None:
        data = self.conn.send(event)
        if not data:
            return
        self.writer.write(data)
        if self.count is not None:
            self.count.down += len(data)
        await self.writer.drain()


class Interceptor:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        addresses: list[str],
        hooks: EgressHooks,
        upstream: ssl.SSLContext,
        count: ByteCount,
    ) -> None:
        self.host = host
        self.port = port
        self.addresses = addresses
        self.hooks = hooks
        self.upstream_context = upstream
        self.count = count
        self.guest: _Side | None = None
        self.server: _Side | None = None

    async def run(self, sock: socket.socket, server_context: ssl.SSLContext) -> str:
        try:
            reader, writer = await accept_stream(
                sock, ssl_context=server_context, handshake_timeout=HANDSHAKE_TIMEOUT
            )
        except (OSError, TimeoutError):
            sock.close()
            return "client_tls"
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEAD_BYTES)
        self.guest = _Side(conn, reader, writer, self.count)
        try:
            return await self._serve()
        finally:
            if self.server is not None:
                await close_writer(self.server.writer)
            await close_writer(writer)

    async def _serve(self) -> str:
        guest = self.guest
        assert guest is not None
        while True:
            try:
                event = await guest.next_event()
            except h11.RemoteProtocolError:
                await self._error(400)
                return "bad_request"
            except (OSError, ConnectionError):
                return ""
            if isinstance(event, h11.ConnectionClosed):
                return ""
            if not isinstance(event, h11.Request):
                return "bad_request"
            try:
                switched = await self._exchange(event)
            except InterceptError as exc:
                if exc.status is not None:
                    await self._error(exc.status)
                return exc.reason
            except (OSError, ConnectionError, h11.ProtocolError):
                return ""
            if switched:
                return ""
            if not self._next_cycle():
                return ""

    def _next_cycle(self) -> bool:
        guest = self.guest
        assert guest is not None
        if (
            guest.conn.our_state is not h11.DONE
            or guest.conn.their_state is not h11.DONE
        ):
            return False
        guest.conn.start_next_cycle()
        server = self.server
        if server is not None:
            conn = server.conn
            if conn.our_state is h11.DONE and conn.their_state is h11.DONE:
                conn.start_next_cycle()
            else:
                server.writer.close()
                self.server = None
        return True

    async def _head(self, event: h11.Request) -> RequestHead:
        head = RequestHead(
            method=_text(event.method),
            target=_text(event.target),
            headers=_headers(event.headers.raw_items()),
            host=self.host,
            port=self.port,
        )
        if head.method == "CONNECT":
            raise InterceptError("connect_method", 405)
        if not (head.target.startswith("/") or head.target == "*"):
            raise InterceptError("bad_target", 400)
        if not host_matches(head):
            raise InterceptError("host_mismatch", 421)
        for hook in self.hooks.request:
            try:
                result = await _resolved(hook(head))
            except Exception as exc:
                raise InterceptError("hook_error", 502) from exc
            if isinstance(result, Reject):
                raise InterceptError(result.reason, result.status)
            if isinstance(result, RequestHead):
                head = result
        if not host_matches(head):
            raise InterceptError("host_mismatch", 421)
        return head

    async def _upstream(self) -> _Side:
        if self.server is not None:
            return self.server
        try:
            reader, writer = await open_upstream(
                self.addresses,
                self.port,
                ssl_context=self.upstream_context,
                server_hostname=self.host,
            )
        except UpstreamError as exc:
            raise InterceptError(exc.reason, 502) from exc
        self.server = _Side(h11.Connection(h11.CLIENT), reader, writer)
        return self.server

    async def _exchange(self, event: h11.Request) -> bool:
        head = await self._head(event)
        server = await self._upstream()
        await server.send(
            h11.Request(
                method=_raw(head.method),
                target=_raw(head.target),
                headers=_wire(head.headers),
            )
        )
        body = asyncio.create_task(self._request_body())
        try:
            switched = await self._response(head)
        except BaseException:
            body.cancel()
            with contextlib.suppress(BaseException):
                await body
            raise
        if not body.done():
            if switched:
                await body
            else:
                body.cancel()
                with contextlib.suppress(BaseException):
                    await body
                raise InterceptError("early_response")
        body.result()
        if switched:
            await self._switch()
        return switched

    async def _request_body(self) -> None:
        guest = self.guest
        server = self.server
        assert guest is not None and server is not None
        while True:
            event = await guest.next_event()
            if isinstance(event, h11.Data):
                await server.send(h11.Data(data=event.data))
            elif isinstance(event, h11.EndOfMessage):
                await server.send(h11.EndOfMessage(headers=event.headers))
                return
            else:
                raise InterceptError("guest_closed")

    async def _response(self, head: RequestHead) -> bool:
        guest = self.guest
        server = self.server
        assert guest is not None and server is not None
        while True:
            event = await server.next_event()
            if isinstance(event, h11.InformationalResponse):
                await guest.send(
                    h11.InformationalResponse(
                        status_code=event.status_code,
                        headers=event.headers.raw_items(),
                        reason=event.reason,
                    )
                )
                if event.status_code == 101:
                    return True
            elif isinstance(event, h11.Response):
                response = await self._response_head(head, event)
                await guest.send(
                    h11.Response(
                        status_code=response.status,
                        headers=_wire(response.headers),
                        reason=_raw(response.reason),
                    )
                )
            elif isinstance(event, h11.Data):
                await guest.send(h11.Data(data=event.data))
            elif isinstance(event, h11.EndOfMessage):
                await guest.send(h11.EndOfMessage(headers=event.headers))
                return False
            else:
                raise InterceptError("upstream_closed", 502)

    async def _response_head(
        self, head: RequestHead, event: h11.Response
    ) -> ResponseHead:
        response = ResponseHead(
            status=event.status_code,
            reason=_text(event.reason),
            headers=_headers(event.headers.raw_items()),
        )
        for hook in self.hooks.response:
            try:
                result = await _resolved(hook(head, response))
            except Exception as exc:
                raise InterceptError("hook_error") from exc
            if isinstance(result, ResponseHead):
                response = result
        return response

    async def _switch(self) -> None:
        guest = self.guest
        server = self.server
        assert guest is not None and server is not None
        to_server, _ = guest.conn.trailing_data
        to_guest, _ = server.conn.trailing_data
        if to_server:
            server.writer.write(to_server)
            self.count.up += len(to_server)
        if to_guest:
            guest.writer.write(to_guest)
            self.count.down += len(to_guest)
        self.server = None
        await splice(
            guest.reader, guest.writer, server.reader, server.writer, self.count
        )

    async def _error(self, status: int) -> None:
        guest = self.guest
        if guest is None:
            return
        body = f"{status} egress gateway\n".encode()
        with contextlib.suppress(Exception):
            await guest.send(
                h11.Response(
                    status_code=status,
                    headers=[
                        (b"content-type", b"text/plain"),
                        (b"content-length", str(len(body)).encode()),
                        (b"connection", b"close"),
                    ],
                )
            )
            await guest.send(h11.Data(data=body))
            await guest.send(h11.EndOfMessage())


async def intercept(
    sock: socket.socket,
    *,
    host: str,
    port: int,
    addresses: list[str],
    server_context: ssl.SSLContext,
    upstream: ssl.SSLContext,
    hooks: EgressHooks,
    count: ByteCount,
) -> str:
    interceptor = Interceptor(
        host=host,
        port=port,
        addresses=addresses,
        hooks=hooks,
        upstream=upstream,
        count=count,
    )
    return await interceptor.run(sock, server_context)
