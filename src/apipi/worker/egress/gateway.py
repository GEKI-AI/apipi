import asyncio
import contextlib
import errno
import logging
import resource
import socket
import struct
import time
from collections.abc import Callable, Iterable

import h11

from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics
from apipi.worker.egress.ca import WorkerCA, worker_ca
from apipi.worker.egress.dns import DnsFilter, Upstream
from apipi.worker.egress.intercept import (
    EgressHooks,
    RequestHook,
    ResponseHook,
    intercept,
    upstream_context,
)
from apipi.worker.egress.policy import EgressPolicy, norm_host, split_host_port
from apipi.worker.egress.resolve import (
    Blocked,
    EgressBlocked,
    Resolver,
    SystemResolver,
    address_blocked,
    check_address,
    resolve_upstream,
)
from apipi.worker.egress.sni import Incomplete, NotTls, parse_client_hello
from apipi.worker.egress.sockets import (
    UpstreamError,
    accept_stream,
    bind_socket,
    open_upstream,
    original_dst,
)
from apipi.worker.egress.splice import IDLE_TIMEOUT, ByteCount, splice

log = logging.getLogger("apipi.egress")

TLS_PORTS = frozenset({443, 8443})
HTTP_PORTS = frozenset({80})
GATEWAY_PORTS = tuple(sorted(TLS_PORTS | HTTP_PORTS))
PEEK_TIMEOUT = 10.0
RESOLVE_TIMEOUT = 5.0
MAX_LOOKUPS = 8
MAX_PEEK = 16384
MAX_CONNECTIONS = 128
WORKER_MAX_CONNECTIONS = 8192
FD_RESERVE = 512
ACCEPT_BACKOFF = 0.5
WARN_INTERVAL = 60.0
DECISIONS = {"splice": "spliced", "intercept": "intercepted", "reject": "rejected"}
FORBIDDEN = (
    b"HTTP/1.1 403 Forbidden\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 37\r\n"
    b"Connection: close\r\n\r\n"
    b"egress gateway: host is not allowed.\n"
)
_FD_ERRORS = frozenset({errno.EMFILE, errno.ENFILE, errno.ENOBUFS, errno.ENOMEM})

OriginalDst = Callable[[socket.socket], tuple[str, int]]

_metrics: Metrics | None = None
_worker_open = 0
_last_warning = 0.0


def set_egress_metrics(metrics: Metrics | None) -> None:
    global _metrics
    _metrics = metrics


def raise_nofile_limit() -> int:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = hard if hard != resource.RLIM_INFINITY else 1 << 20
    if soft == resource.RLIM_INFINITY or soft >= target:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError):
        return soft
    return target


def worker_connection_limit() -> int:
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY:
        return WORKER_MAX_CONNECTIONS
    return max(16, min(WORKER_MAX_CONNECTIONS, (soft - FD_RESERVE) // 3))


def _warn_accept(session_id: str | None, error: str) -> None:
    global _last_warning
    now = time.monotonic()
    if now - _last_warning < WARN_INTERVAL:
        return
    _last_warning = now
    log_event(
        log,
        logging.WARNING,
        "egress gateway cannot accept",
        event="egress.accept.failed",
        session_id=session_id,
        error=error,
    )


def egress_metrics() -> Metrics | None:
    return _metrics


def _tls_enough(data: bytes) -> bool:
    try:
        parse_client_hello(data)
    except Incomplete:
        return False
    except NotTls:
        return True
    return True


def _http_enough(data: bytes) -> bool:
    if b"\r\n\r\n" in data or b"\n\n" in data:
        return True
    method, space, _ = data.partition(b" ")
    if space:
        return not method.isalpha()
    return len(method) > 16 or not method.isalpha()


def _http_host(data: bytes) -> tuple[str | None, bool]:
    conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_PEEK)
    try:
        conn.receive_data(data)
        event = conn.next_event()
    except h11.RemoteProtocolError:
        return None, False
    if not isinstance(event, h11.Request):
        return None, False
    for name, value in event.headers:
        if name == b"host":
            try:
                host, _ = split_host_port(value.decode("latin-1"))
            except ValueError:
                return None, True
            return norm_host(host) or None, True
    return None, True


class _Connection:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.count = ByteCount()
        self.host: str | None = None
        self.port = 0
        self.action = "reject"
        self.reason = ""
        self.http = False
        self.recognized = False


class EgressGateway:
    def __init__(
        self,
        *,
        host: str,
        policy: EgressPolicy,
        ca: WorkerCA | None = None,
        session_id: str | None = None,
        upstream_ca: str | None = None,
        hooks: EgressHooks | None = None,
        resolve: Resolver | None = None,
        original_dst: OriginalDst = original_dst,
        blocked: Blocked = address_blocked,
        dns_upstreams: tuple[Upstream, ...] = (),
        metrics: Metrics | None = None,
        tls_ports: frozenset[int] = TLS_PORTS,
        http_ports: frozenset[int] = HTTP_PORTS,
        peek_timeout: float = PEEK_TIMEOUT,
        idle_timeout: float = IDLE_TIMEOUT,
        max_connections: int = MAX_CONNECTIONS,
        resolve_timeout: float = RESOLVE_TIMEOUT,
        max_lookups: int = MAX_LOOKUPS,
        freebind: bool = False,
    ) -> None:
        self.host = host
        self.policy = policy
        self.ca = ca if ca is not None else worker_ca()
        self.session_id = session_id
        self.hooks = hooks if hooks is not None else EgressHooks()
        self.upstream = upstream_context(upstream_ca)
        self._system: SystemResolver | None = None
        if resolve is None:
            self._system = SystemResolver(max_lookups)
            resolve = self._system
        self.resolve = resolve
        self.original_dst = original_dst
        self.blocked = blocked
        self.dns_upstreams = dns_upstreams
        self.metrics = metrics
        self.tls_ports = tls_ports
        self.http_ports = http_ports
        self.peek_timeout = peek_timeout
        self.idle_timeout = idle_timeout
        self.max_connections = max_connections
        self.freebind = freebind
        self.resolve_timeout = resolve_timeout
        self._lookups = asyncio.Semaphore(max_lookups)
        self.port = 0
        self.dns: DnsFilter | None = None
        self._listener: socket.socket | None = None
        self._rearm: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def ca_pem(self) -> bytes:
        return self.ca.cert_pem

    @property
    def dns_ports(self) -> tuple[int, int] | None:
        if self.dns is None:
            return None
        return self.dns.udp_port, self.dns.tcp_port

    def set_intercept_hosts(self, hosts: Iterable[str]) -> None:
        self.policy = self.policy.with_intercept(hosts)

    def add_request_hook(self, hook: RequestHook) -> None:
        self.hooks.request.append(hook)

    def add_response_hook(self, hook: ResponseHook) -> None:
        self.hooks.response.append(hook)

    def set_session(self, session_id: str | None) -> None:
        self.session_id = session_id

    async def start(self) -> None:
        listener = bind_socket(self.host, socket.SOCK_STREAM, freebind=self.freebind)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._arm()
        if self.policy.mode == "restricted":
            self.dns = DnsFilter(
                host=self.host,
                allow=lambda name: self.policy.allows_name(name),
                private=lambda name: self.policy.private_name(name),
                upstreams=self.dns_upstreams,
                freebind=self.freebind,
            )
            try:
                await self.dns.start()
            except OSError:
                self.close()
                raise

    def _arm(self) -> None:
        self._rearm = None
        if self._listener is not None:
            loop = asyncio.get_running_loop()
            loop.add_reader(self._listener.fileno(), self._accept)

    def close(self) -> None:
        if self._rearm is not None:
            self._rearm.cancel()
            self._rearm = None
        listener = self._listener
        if listener is not None:
            self._listener = None
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(listener.fileno())
            listener.close()
        if self.dns is not None:
            self.dns.close()
        if self._system is not None:
            self._system.close()
        for task in list(self._tasks):
            task.cancel()

    async def stop(self) -> None:
        tasks = list(self._tasks)
        self.close()
        if self.dns is not None:
            await self.dns.stop()
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task

    def _backoff(self, exc: OSError) -> None:
        listener = self._listener
        if listener is None:
            return
        loop = asyncio.get_running_loop()
        loop.remove_reader(listener.fileno())
        self._rearm = loop.call_later(ACCEPT_BACKOFF, self._arm)
        _warn_accept(self.session_id, exc.strerror or str(exc))

    def _accept(self) -> None:
        global _worker_open
        listener = self._listener
        if listener is None:
            return
        while True:
            try:
                sock, _ = listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                if exc.errno in _FD_ERRORS:
                    self._backoff(exc)
                    return
                continue
            if (
                len(self._tasks) >= self.max_connections
                or _worker_open >= worker_connection_limit()
            ):
                _reset(sock)
                continue
            sock.setblocking(False)
            _worker_open += 1
            task = asyncio.create_task(self._handle(sock))
            self._tasks.add(task)
            task.add_done_callback(self._done)

    def _done(self, task: "asyncio.Task[None]") -> None:
        global _worker_open
        _worker_open -= 1
        self._tasks.discard(task)

    async def _handle(self, sock: socket.socket) -> None:
        conn = _Connection(sock)
        owned = True
        try:
            try:
                dest, conn.port = self.original_dst(sock)
            except OSError:
                conn.reason = "no_destination"
                return
            tls = conn.port in self.tls_ports
            conn.http = conn.port in self.http_ports
            if not tls and not conn.http:
                conn.reason = "port"
                return
            data = await self._peek(sock, _tls_enough if tls else _http_enough)
            if data == b"":
                conn.reason = "no_data"
                return
            if data is not None:
                self._read_host(conn, data, tls)
            restricted = self.policy.mode == "restricted"
            if not conn.recognized and restricted:
                if data is None:
                    conn.reason = "timeout"
                else:
                    conn.reason = "not_tls" if tls else "not_http"
                return
            decision = self.policy.decide(conn.host, conn.port)
            if decision.action == "reject":
                conn.reason = decision.reason
                return
            if decision.action == "intercept" and not tls:
                conn.reason = "credential_host_plain_http"
                return
            owns_name = conn.host is not None and (
                restricted or decision.action == "intercept"
            )
            if owns_name and conn.host is not None:
                private = (
                    self.policy.private_hosts
                    if self.policy.private_allowed(conn.host)
                    else ()
                )
                addresses = await resolve_upstream(
                    conn.host,
                    conn.port,
                    private_hosts=private,
                    resolve=self._resolve,
                    blocked=self.blocked,
                )
            else:
                addresses = check_address(dest, blocked=self.blocked)
            if conn.host is not None and (
                (decision.action == "intercept" and tls) or (conn.http and restricted)
            ):
                conn.action = "intercept"
                owned = False
                conn.reason = await intercept(
                    sock,
                    host=conn.host,
                    port=conn.port,
                    addresses=addresses,
                    server_context=self.ca.server_context(conn.host) if tls else None,
                    upstream=self.upstream if tls else None,
                    hooks=self.hooks if tls else EgressHooks(),
                    count=conn.count,
                    idle_timeout=self.idle_timeout,
                )
                return
            upstream_reader, upstream_writer = await open_upstream(addresses, conn.port)
            conn.action = "splice"
            try:
                guest_reader, guest_writer = await accept_stream(sock)
            except BaseException:
                upstream_writer.close()
                raise
            owned = False
            await splice(
                guest_reader,
                guest_writer,
                upstream_reader,
                upstream_writer,
                conn.count,
                idle_timeout=self.idle_timeout,
            )
        except EgressBlocked as exc:
            conn.action = "reject"
            conn.reason = exc.reason
        except UpstreamError as exc:
            conn.action = "reject"
            conn.reason = exc.reason
        except (OSError, ConnectionError):
            conn.reason = conn.reason or "closed"
        except Exception:
            log.exception("egress connection failed")
            conn.reason = "error"
        finally:
            if owned:
                self._close(conn)
            self._record(conn)

    async def _resolve(self, host: str, port: int) -> list[str]:
        async with asyncio.timeout(self.resolve_timeout):
            await self._lookups.acquire()
            try:
                lookup = asyncio.ensure_future(self.resolve(host, port))
            except BaseException:
                self._lookups.release()
                raise
            lookup.add_done_callback(self._lookup_done)
            return await asyncio.shield(lookup)

    def _lookup_done(self, lookup: "asyncio.Future[list[str]]") -> None:
        self._lookups.release()
        if not lookup.cancelled():
            lookup.exception()

    def _close(self, conn: _Connection) -> None:
        sock = conn.sock
        if conn.action != "reject":
            sock.close()
            return
        if not (conn.http and conn.recognized):
            _reset(sock)
            return
        with contextlib.suppress(OSError):
            sock.recv(MAX_PEEK)
            sock.send(FORBIDDEN)
            sock.shutdown(socket.SHUT_WR)
        sock.close()

    def _read_host(self, conn: _Connection, data: bytes, tls: bool) -> None:
        if tls:
            try:
                hello = parse_client_hello(data)
            except (Incomplete, NotTls):
                return
            conn.recognized = True
            conn.host = hello.server_name
            return
        conn.host, conn.recognized = _http_host(data)

    async def _peek(
        self, sock: socket.socket, enough: Callable[[bytes], bool]
    ) -> bytes | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.peek_timeout
        delay = 0.001
        seen = -1
        while True:
            try:
                data = sock.recv(MAX_PEEK, socket.MSG_PEEK)
            except (BlockingIOError, InterruptedError):
                try:
                    await _readable(sock, deadline - loop.time())
                except TimeoutError:
                    return None
                continue
            if not data:
                return b""
            if len(data) >= MAX_PEEK or enough(data):
                return data
            if loop.time() >= deadline:
                return data
            if len(data) == seen:
                delay = min(delay * 2, 0.05)
            else:
                delay = 0.001
                seen = len(data)
            await asyncio.sleep(delay)

    def _record(self, conn: _Connection) -> None:
        decision = DECISIONS[conn.action]
        log_event(
            log,
            logging.INFO,
            "egress connection",
            event="egress.connection",
            session_id=self.session_id,
            host=conn.host,
            port=conn.port,
            decision=decision,
            reason=conn.reason or None,
            bytes_up=conn.count.up,
            bytes_down=conn.count.down,
        )
        metrics = self.metrics if self.metrics is not None else _metrics
        if metrics is not None:
            metrics.observe_egress_connection(
                decision, bytes_up=conn.count.up, bytes_down=conn.count.down
            )


async def _readable(sock: socket.socket, timeout: float) -> None:
    if timeout <= 0:
        raise TimeoutError
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not ready.done():
            ready.set_result(None)

    fd = sock.fileno()
    loop.add_reader(fd, wake)
    try:
        await asyncio.wait_for(ready, timeout)
    finally:
        loop.remove_reader(fd)


def _reset(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
