import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from pydantic import ValidationError

from apipi import __version__
from apipi.common.background import spawn_loop, start_event_loop_lag, watch_task
from apipi.common.logutil import (
    RateLimitedLog,
    bind_log_context,
    log_event,
    unbind_log_context,
)
from apipi.common.wirewatch import note_unknown_fields, note_unknown_type
from apipi.config import (
    ConfigError,
    Settings,
    load_worker_token,
    reject_legacy_worker_token,
    reject_worker_database_url,
)
from apipi.protocol import (
    COMMAND_OPS,
    CURSOR_OPS,
    FEATURE_SEARCH,
    FEATURE_SESSION_STOPPED,
    INVALID_REGISTER_REASON,
    PROTOCOL_VERSION,
    REGISTER_REQUIRED_REASON,
    REVOKED_REASON,
    SHARED_STORE_REASON,
    SUPPORTED_FEATURES,
    TOKEN_BOUND_REASON,
    UNAUTHORIZED_REASON,
    UNSUPPORTED_PROTOCOL_REASON,
    ArtifactPresignReply,
    ContextCommandPayload,
    CumulativeAck,
    HeartbeatMessage,
    HelloReply,
    InventoryEntry,
    InventoryMessage,
    InventoryReply,
    LeaseAck,
    LeaseRelease,
    LeaseRevoke,
    RegisterMessage,
    RunningSession,
    SandboxSeenMessage,
    SearchReply,
    StoreCheck,
    StoreProof,
    WireModel,
    WorkerCommand,
    WorkerErrorPayload,
    WorkerImageInfo,
    collect_unknown_fields,
    dumps_wire,
    parse_api_message,
    peer_features,
    wire_bytes,
    wire_type,
)
from apipi.worker.artifact_upload import (
    fail_lost_presign_waiters,
    handle_presign_reply,
)
from apipi.worker.commands import CommandDedupe, dispatch_command, log_command
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
from apipi.worker.inventory import (
    _seed_reaper_ttl,
    _unleased_session_dirs,
    finish_revoke,
    forget_revoked,
    revoked_sessions,
)
from apipi.worker.outbox import SPOOL_FSYNC_SECONDS, Outbox

log = logging.getLogger("apipi.worker")

INVENTORY_INTERVAL = 60.0
RELEASE_FLUSH_TIMEOUT = 10.0
HELLO_TIMEOUT = 15.0
PING_INTERVAL = 5.0
PING_TIMEOUT = 10.0
WS_KEEPALIVE: dict[str, Any] = {
    "ping_interval": PING_INTERVAL,
    "ping_timeout": PING_TIMEOUT,
}
RECONNECT_BASE = 0.5
RECONNECT_CAP = 10.0
RECONNECT_RESET_AFTER = 30.0
PUMP_BATCH = 64
SHUTDOWN_HARVEST_TIMEOUT = 30.0
FATAL_REJECTIONS = frozenset(
    {
        UNAUTHORIZED_REASON,
        REVOKED_REASON,
        UNSUPPORTED_PROTOCOL_REASON,
        TOKEN_BOUND_REASON,
        SHARED_STORE_REASON,
        REGISTER_REQUIRED_REASON,
        "register required",
        INVALID_REGISTER_REASON,
        "invalid register",
    }
)


def answer_store_check(settings: Any, hello: dict[str, Any]) -> dict[str, Any] | None:
    """Build the `store.proof` reply for a hello challenge, if any.

    Raises ConfigError with the documented shared-path message when the
    worker cannot read the marker back.
    """
    from apipi.common.dirs import store_root
    from apipi.common.store_check import SHARED_STORE_ERROR, read_store_check
    from apipi.config import ConfigError

    try:
        check = StoreCheck.model_validate(hello.get("store_check"))
    except ValidationError:
        return None
    if not read_store_check(store_root(settings), check.marker, check.nonce):
        raise ConfigError(SHARED_STORE_ERROR)
    return StoreProof(marker=check.marker, nonce=check.nonce).to_wire()


def worker_ws_url(base: str) -> str:
    parsed = urlparse(base)
    if parsed.scheme in {"http", "https"}:
        scheme = "wss" if parsed.scheme == "https" else "ws"
        parsed = parsed._replace(scheme=scheme)
    elif parsed.scheme not in {"ws", "wss"}:
        raise ConfigError("APIPI_API_URL must be an http URL")
    path = parsed.path.rstrip("/") + "/internal/worker"
    return urlunparse(parsed._replace(path=path, fragment=""))


def worker_arch() -> str:
    return os.uname().machine


def _worker_connect_kwargs(settings: Settings, ws_url: str) -> dict[str, Any]:
    """Extra kwargs for the worker's `websockets.connect` call.

    `ssl` is only passed when there is a real context: websockets
    raises for `ssl=None` on a `wss://` URI, and omitting it already
    verifies against the system trust store. Plain `ws://`
    (loopback only) needs no context."""
    from apipi.worker.tls import worker_ssl_context

    context = worker_ssl_context(settings) if ws_url.startswith("wss") else None
    return {"ssl": context} if context is not None else {}


def _heartbeat_images(settings: Settings) -> list[dict[str, str]]:
    from apipi.worker.accepts import resolved_worker_accepts

    if "microvm" not in resolved_worker_accepts(settings):
        return []
    from apipi.common.images import available_images

    return [
        {
            "id": item.id,
            "version": item.version,
            "digest": item.digest,
            "min_size": item.min_size,
        }
        for item in available_images(settings)
    ]


def worker_heartbeat(settings: Settings, *, drain: bool = False) -> dict[str, object]:
    from apipi.worker.accepts import resolved_worker_accepts

    heartbeat = HeartbeatMessage(
        capacity=settings.max_sessions,
        memory_mb=settings.node_memory_mb(),
        run_mode=settings.run_mode,
        accepts=sorted(resolved_worker_accepts(settings)),
        arch=worker_arch(),
        image_store_version=settings.image_store_version or "",
        images=[
            WorkerImageInfo.model_validate(item) for item in _heartbeat_images(settings)
        ],
    )
    if drain:
        heartbeat.drain = True
    return heartbeat.to_wire()


def drain_idle(live: int, command_tasks: set[asyncio.Task[None]]) -> bool:
    return live == 0 and not command_tasks


def drain_timeout_seconds(settings: Settings, drain_timeout: float | None) -> float:
    if drain_timeout is not None:
        return drain_timeout
    return settings.idle_ttl.total_seconds()


def _install_drain_signals(draining: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_drain() -> None:
        draining.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, request_drain)


def reconnect_delay(attempt: int, *, rng: Callable[[], float] | None = None) -> float:
    """Full jitter: a random delay up to the exponential ceiling, capped."""
    ceiling = min(RECONNECT_CAP, RECONNECT_BASE * 2 ** min(max(attempt - 1, 0), 16))
    return (rng or random.random)() * ceiling


class HelloTimeout(Exception):
    """The API accepted the socket but sent no `hello.reply` in time."""


class BadHello(Exception):
    """The first frame was not a usable `hello.reply`; the worker reconnects."""


@dataclass
class ConnectionState:
    ready_at: float | None = None


def _start_drain(execution: Any, metrics: Any, wait: float) -> float:
    deadline = time.monotonic() + wait
    if metrics is not None:
        metrics.set_worker_draining(True)
    log_event(
        log,
        logging.INFO,
        "worker drain started",
        event="worker.drain.started",
        sessions=execution.pool.live(),
        timeout_s=wait,
    )
    return deadline


def _finish_drain(outbox: "Outbox", result: str, execution: Any) -> None:
    unacked = len(outbox.pending_sessions())
    if result == "drained":
        log_event(
            log,
            logging.INFO,
            "worker drain finished",
            event="worker.drain.finished",
            result=result,
        )
        return
    log_event(
        log,
        logging.WARNING,
        "worker drain timed out",
        event="worker.drain.finished",
        error_code="drain_timeout",
        result=result,
        sessions=execution.pool.live(),
        unacked_sessions=unacked,
        unacked_envelopes=outbox.describe()["messages"],
    )


async def run_worker(
    settings: Settings,
    *,
    url: str | None = None,
    drain_timeout: float | None = None,
    connect: Any | None = None,
) -> int:
    """Run the split worker event loop.

    `connect` is an optional factory for tests: it is called as
    `connect(ws_url, additional_headers=..., **connect_kwargs)` and must
    return an async context manager yielding a socket with the `send` /
    `recv` methods `_serve_connection` uses. Production default stays
    `websockets.connect`.
    """
    from apipi.common.event_bus import InMemoryEventBus
    from apipi.worker.accepts import require_worker_accepts
    from apipi.worker.execution import local_execution, worker_observability
    from apipi.worker.lifecycle import OutboxLifecycleReporter, worker_lifecycle_ignored
    from apipi.worker.tls import check_worker_mtls_files, require_worker_tls

    reject_legacy_worker_token()
    reject_worker_database_url()
    require_worker_accepts(settings)
    ignored_lifecycle = worker_lifecycle_ignored()
    if ignored_lifecycle:
        log.warning(
            "worker ignores API-only lifecycle settings",
            extra={
                "event": "worker.lifecycle.ignored",
                "settings": ignored_lifecycle,
            },
        )
    token = load_worker_token(settings.worker_token_file)
    base = url or settings.api_url or "http://127.0.0.1:8000"
    ws_url = require_worker_tls(base)
    check_worker_mtls_files(settings)
    if "worker_lease_ttl" in settings.model_fields_set:
        log.warning(
            "worker ignores APIPI_WORKER_LEASE_TTL; the API sets the lease TTL",
            extra={"event": "worker.lease_ttl.ignored"},
        )
    metrics, tracing = worker_observability(settings)
    # A split worker holds no database and no object-store credentials.
    # Turn context arrives in commands, live deltas go over the socket
    # through the relay, and durable results go through the outbox
    # with a cumulative ack, so the event bus is always in memory.
    bus = InMemoryEventBus()
    relay = DeltaRelay()
    outbox = worker_outbox(settings)
    outbox.metrics = metrics
    relay.metrics = metrics
    execution = local_execution(
        settings,
        hub=LiveRedirectBus(bus, relay),
        metrics=metrics,
        tracing=tracing,
        outbox=outbox,
    )
    execution.socket_open = False
    # Split workers report lifecycle over the socket; the API owns the
    # export. The reporter only appends durable envelopes, and the
    # reaper learns idle TTLs from inventory replies, so no background
    # loop needs the database.
    execution.pool.lifecycle = OutboxLifecycleReporter(outbox)
    await bus.start()
    tasks: set[asyncio.Task[None]] = set()
    if metrics is not None:
        from apipi.worker.scrape import serve_metrics

        metrics_task = asyncio.create_task(
            serve_metrics(
                metrics,
                host=settings.worker_metrics_host,
                port=settings.worker_metrics_port,
            ),
            name="worker_metrics",
        )
        watch_task(metrics_task, "worker_metrics", metrics=metrics)
        tasks.add(metrics_task)
        log.info(
            "worker metrics",
            extra={
                "host": settings.worker_metrics_host,
                "port": settings.worker_metrics_port,
            },
        )
    for name, factory in (
        ("worker_observe", execution.observe_loop),
        ("session_reaper", execution.reap_loop),
        ("workspace_reaper", execution.reap_workspace_loop),
        ("sandbox_seen", execution.sandbox_seen_loop),
    ):
        loop_task = asyncio.create_task(factory(), name=name)
        watch_task(loop_task, name, metrics=metrics)
        tasks.add(loop_task)
    tasks.add(start_event_loop_lag(metrics))
    if metrics is not None:

        async def observe_outbox() -> None:
            outbox.observe()

        tasks.add(
            spawn_loop("outbox_metrics", observe_outbox, interval=1.0, metrics=metrics)
        )
    if outbox.spool_dir is not None:
        tasks.add(
            spawn_loop(
                "outbox_spool",
                outbox.maintain_spool,
                interval=SPOOL_FSYNC_SECONDS,
                metrics=metrics,
            )
        )
    emitter = getattr(getattr(execution, "pool", None), "lifecycle", None)
    if emitter is not None:
        emitter.start()
    log.info("worker connect", extra={"url": ws_url})
    spooled = outbox.load_spool()
    if spooled:
        log_event(
            log,
            logging.INFO,
            "worker outbox spool recovered",
            event="worker.spool.recovered",
            sessions=len(spooled),
            envelopes=outbox.describe()["messages"],
            bytes=outbox.describe()["bytes"],
            skipped_lines=outbox.spool_skipped,
            spool_bytes=outbox.spool_size(),
        )
    draining = asyncio.Event()
    _install_drain_signals(draining)
    drain_deadline: float | None = None
    wait = drain_timeout_seconds(settings, drain_timeout)
    command_tasks: set[asyncio.Task[None]] = set()
    session_leases: dict[uuid.UUID, str] = {}
    # Leases the worker let go while no socket was open. The release
    # goes out after the next hello, so the API does not see a claim
    # that vanished and does not orphan an idle session.
    pending_releases: dict[uuid.UUID, str] = {}
    # Command dedupe is worker-lifetime, not per-connection: the API
    # replays unacked commands after a reconnect with the same
    # `command_id`, and only a dedupe that survives the socket tells
    # the replay from a new turn. Entries are forgotten when the
    # session is torn down, stopped, revoked, or released.
    dedupe = CommandDedupe()

    async def release_offline(session_id: uuid.UUID) -> None:
        relay.forget(session_id)
        lease_id = session_leases.pop(session_id, None)
        if lease_id is not None:
            pending_releases[session_id] = lease_id
        dedupe.forget(session_id)

    status = 0
    attempt = 0
    attempted = False
    warnings = RateLimitedLog(log)
    connect_factory = connect if connect is not None else websockets.connect
    try:
        while True:
            connect_started = time.monotonic()
            state = ConnectionState()
            execution.note_stopped = release_offline
            try:
                try:
                    connect_kwargs = _worker_connect_kwargs(settings, ws_url)
                except ConfigError as exc:
                    if not attempted:
                        raise
                    raise BadHello(f"TLS context could not be rebuilt: {exc}") from exc
                attempted = True
                async with connect_factory(
                    ws_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    **WS_KEEPALIVE,
                    **connect_kwargs,
                ) as sock:
                    outcome, drain_deadline = await _serve_connection(
                        settings,
                        execution,
                        outbox,
                        relay,
                        sock,
                        session_leases,
                        command_tasks,
                        tasks,
                        draining,
                        drain_deadline,
                        wait,
                        emitter,
                        dedupe,
                        connect_started=connect_started,
                        state=state,
                        pending_releases=pending_releases,
                    )
                if outcome == "drained":
                    break
                if outcome == "drain_timeout":
                    status = 1
                    break
            except ConfigError:
                raise
            except Exception as exc:
                if metrics is not None:
                    metrics.set_worker_connected(False)
                if (
                    state.ready_at is not None
                    and time.monotonic() - state.ready_at >= RECONNECT_RESET_AFTER
                ):
                    attempt = 0
                reason = _reconnect_reason(exc)
                warnings.warning(
                    "worker connection lost, reconnecting",
                    event="worker.connection.lost",
                    error_code=reason,
                    exc_info=True,
                    reason=reason,
                )
                attempt += 1
                delay = reconnect_delay(attempt)
                if draining.is_set():
                    if drain_deadline is None:
                        drain_deadline = _start_drain(execution, metrics, wait)
                    try:
                        await execution.pool.kill_unheld(reason="drain")
                    except Exception:
                        log.exception("worker drain kill failed")
                    if (
                        drain_idle(execution.pool.live(), command_tasks)
                        and not outbox.pending_sessions()
                    ):
                        _finish_drain(outbox, "drained", execution)
                        break
                    remaining = drain_deadline - time.monotonic()
                    if remaining <= 0:
                        _finish_drain(outbox, "drain_timeout", execution)
                        status = 1
                        break
                    delay = min(delay, remaining)
                if metrics is not None:
                    metrics.observe_worker_reconnect(reason)
                log_event(
                    log,
                    logging.INFO,
                    "worker reconnecting",
                    event="worker.reconnecting",
                    reason=reason,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                )
                await asyncio.sleep(delay)
                continue
            finally:
                execution.note_stopped = release_offline
    finally:
        relay.detach()
        for task in tasks:
            task.cancel()
        await execution.close()
        outbox.sync_spool()
        if execution.tracing is not None:
            execution.tracing.shutdown()
        await bus.close()
    return status


def _reconnect_reason(exc: BaseException) -> str:
    if isinstance(exc, HelloTimeout):
        return "hello_timeout"
    if isinstance(exc, websockets.ConnectionClosed):
        sent = exc.sent
        if sent is not None and "keepalive ping timeout" in (sent.reason or ""):
            return "ping_timeout"
        return "closed"
    if isinstance(exc, OSError | TimeoutError | websockets.InvalidHandshake):
        return "connect_error"
    return "error"


def worker_outbox(settings: Settings) -> "Outbox":
    """Build the worker outbox from settings."""
    from apipi.worker.outbox import Outbox

    return Outbox(
        max_messages=settings.worker_outbox_max_messages,
        max_bytes=settings.worker_outbox_max_bytes,
        spool_dir=settings.worker_outbox_dir,
    )


async def _pump_outbox(outbox: "Outbox", send: Any) -> None:
    """Send each buffered durable envelope once per connection (one writer).

    Sessions take turns, `PUMP_BATCH` envelopes at a time, so one noisy
    session cannot hold the others back.
    """
    while True:
        await outbox.wait_dirty()
        for session_id in outbox.unsent_sessions():
            for _ in range(PUMP_BATCH):
                envelope = outbox.next_unsent(session_id)
                if envelope is None:
                    break
                await send(envelope)
                outbox.mark_sent(session_id, envelope)
            else:
                outbox.mark_dirty()


def _running_claim(
    session_leases: dict[uuid.UUID, str], outbox: "Outbox"
) -> list[RunningSession]:
    return [
        RunningSession(
            session_id=session_id,
            lease_id=uuid.UUID(lease_id),
            last_seq=outbox.high_water(session_id),
        )
        for session_id, lease_id in session_leases.items()
    ]


def _reconcile_hello(
    outbox: "Outbox",
    relay: Any,
    session_leases: dict[uuid.UUID, str],
    sessions: Mapping[Any, Any],
    dedupe: "CommandDedupe | None" = None,
) -> list[uuid.UUID]:
    """Adopt the API cursors; forget leases the API no longer holds.

    Returns the sessions whose guests the caller must tear down. The
    teardown can wait for a presign reply, so it never runs here.
    Buffers without a lease claim stay: they are spooled results from
    before a restart, and the API acks or rejects them.
    """
    kept: set[uuid.UUID] = set()
    for raw_id, last_seq in sessions.items():
        try:
            session_id = uuid.UUID(str(raw_id))
            cursor = int(last_seq)
        except (ValueError, TypeError):
            continue
        kept.add(session_id)
        outbox.set_base(session_id, max(cursor, 0))
    dropped = [sid for sid in session_leases if sid not in kept]
    for session_id in dropped:
        session_leases.pop(session_id, None)
        relay.forget(session_id)
        if dedupe is not None:
            dedupe.forget(session_id)
    return dropped


def _adopt_cursor(outbox: "Outbox", command: WorkerCommand) -> None:
    """Continue the session sequence from the cursor the API put in a command."""
    if command.op not in CURSOR_OPS:
        return
    try:
        payload = command.parsed_payload()
    except ValidationError:
        payload = None
    cursor = payload.last_seq if isinstance(payload, ContextCommandPayload) else None
    if cursor is None:
        log_event(
            log,
            logging.WARNING,
            "worker command has no sequence cursor",
            event="worker.command.cursor_missing",
            session_id=command.session_id,
            op=command.op,
        )
        return
    outbox.set_base(command.session_id, cursor)


async def _outbox_flushed(
    outbox: "Outbox",
    session_id: uuid.UUID,
    *,
    timeout: float,
    active: Any,
) -> bool:
    """Wait until the API acked everything buffered for the session so far."""
    target = outbox.high_water(session_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while outbox.acked_seq(session_id) < target:
        if not active():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _parse_hello(raw: Any) -> dict[str, Any]:
    """Decode the first frame. Rejections stop the worker, anything else retries."""
    try:
        text = raw if isinstance(raw, str) else raw.decode()
        hello = json.loads(text)
    except ValueError as exc:
        raise BadHello("first frame is not JSON") from exc
    if not isinstance(hello, dict):
        raise BadHello("first frame is not an object")
    if not hello.get("ok"):
        error = hello.get("error")
        if isinstance(error, str) and error in FATAL_REJECTIONS:
            raise ConfigError(f"worker register failed: {error}")
        raise BadHello(f"first frame is not hello: {error or 'no ok'}")
    return hello


async def _serve_connection(
    settings: Settings,
    execution: Any,
    outbox: "Outbox",
    relay: Any,
    sock: Any,
    session_leases: dict[uuid.UUID, str],
    command_tasks: set[asyncio.Task[None]],
    tasks: set[asyncio.Task[None]],
    draining: asyncio.Event,
    drain_deadline: float | None,
    wait: float,
    emitter: Any,
    dedupe: CommandDedupe,
    *,
    connect_started: float | None = None,
    state: ConnectionState | None = None,
    pending_releases: dict[uuid.UUID, str] | None = None,
) -> tuple[str, float | None]:
    """Serve one socket; returns drained or drain_timeout (loss raises).

    `dedupe` is worker-lifetime (owned by `run_worker`): reconnects
    replay unacked commands with the same `command_id`, so only a
    dedupe that survives the socket suppresses the second dispatch.

    The receive loop only parses and dispatches. Anything that can wait
    for a reply (a command, a guest teardown, a lease release) runs as
    a task, so acks, presign replies, and pongs keep flowing. Heartbeats,
    the inventory, and the drain check run on their own timers, so a
    socket that never goes quiet cannot starve them. The heartbeat
    interval and the lease TTL come from the API in `hello`.
    """
    from apipi.worker.accepts import resolved_worker_accepts

    if pending_releases is None:
        pending_releases = {}
    send_lock = asyncio.Lock()
    metrics = getattr(execution, "metrics", None)
    warnings = RateLimitedLog(log)
    opened = time.monotonic()
    if connect_started is None:
        connect_started = opened
    context_token: Any = None
    end_reason = "closed"

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await sock.send(dumps_wire(payload))

    async def send_message(message: WireModel) -> None:
        await send_json(message.to_wire())

    claimed = set(session_leases)
    await send_message(
        RegisterMessage(
            protocol=PROTOCOL_VERSION,
            capabilities={},
            accepts=sorted(resolved_worker_accepts(settings)),
            running=_running_claim(session_leases, outbox),
            capacity=settings.max_sessions,
            memory_mb=settings.node_memory_mb(),
            run_mode=settings.run_mode,
            arch=worker_arch(),
            version=__version__,
            features=sorted(SUPPORTED_FEATURES),
            images=[
                WorkerImageInfo.model_validate(item)
                for item in _heartbeat_images(settings)
            ],
        )
    )
    try:
        raw = await asyncio.wait_for(sock.recv(), HELLO_TIMEOUT)
    except TimeoutError as exc:
        raise HelloTimeout("no hello.reply within the hello timeout") from exc
    hello = _parse_hello(raw)
    try:
        welcome = HelloReply.model_validate(hello)
    except ValidationError as exc:
        raise ConfigError(f"worker register failed: invalid hello: {exc}") from exc
    heartbeat = welcome.heartbeat_seconds
    lease_ttl = welcome.lease_ttl_seconds
    api_features = peer_features(welcome.features)
    outbox.peer_features = api_features
    context_token = bind_log_context(
        worker_id=hello.get("worker_id"), connection_id=welcome.connection_id
    )
    if state is not None:
        state.ready_at = time.monotonic()
    if metrics is not None:
        metrics.observe_worker_connect_seconds(time.monotonic() - connect_started)
        metrics.set_worker_connected(True)
    proof = answer_store_check(settings, hello)
    if proof is not None:
        await send_json(proof)
    hello_sessions = welcome.sessions
    log_event(
        log,
        logging.INFO,
        "worker hello",
        event="worker.hello.received",
        sessions={str(key): value for key, value in hello_sessions.items()},
        lease_ttl_seconds=lease_ttl,
        heartbeat_seconds=heartbeat,
        revoked=len(welcome.revoke),
        features=sorted(api_features),
        connect_s=round(time.monotonic() - connect_started, 3),
    )
    stopping: set[uuid.UUID] = set()
    teardowns: dict[uuid.UUID, asyncio.Task[None]] = {}

    def spawn(coro: Any, name: str, *, command: bool = False) -> "asyncio.Task[None]":
        task = asyncio.create_task(coro, name=name)
        watch_task(task, name, metrics=metrics)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        if command:
            command_tasks.add(task)
            task.add_done_callback(command_tasks.discard)
        return task

    async def settled(session_id: uuid.UUID) -> None:
        pending = teardowns.get(session_id)
        if pending is not None and not pending.done():
            await asyncio.wait({pending})

    def schedule_teardown(session_id: uuid.UUID, known: bool) -> None:
        previous = teardowns.get(session_id)

        async def run() -> None:
            if previous is not None and not previous.done():
                await asyncio.wait({previous})
            await finish_revoke(
                execution,
                session_id,
                known=known,
                settings=settings,
                outbox=outbox,
            )

        task = spawn(run(), "worker_revoke")
        teardowns[session_id] = task

        def done(finished: "asyncio.Task[None]") -> None:
            if teardowns.get(session_id) is finished:
                del teardowns[session_id]

        task.add_done_callback(done)

    async def flush_outbox(
        session_id: uuid.UUID,
        lease_id: str,
        action: str,
        *,
        active: Any | None = None,
    ) -> None:
        flushed = await _outbox_flushed(
            outbox,
            session_id,
            timeout=RELEASE_FLUSH_TIMEOUT,
            active=active or (lambda: session_leases.get(session_id) == lease_id),
        )
        if not flushed:
            warnings.warning(
                "worker outbox not acked before release",
                event="worker.release.unflushed",
                error_code="release_unflushed",
                session_id=session_id,
                action=action,
                acked_seq=outbox.acked_seq(session_id),
                high_water=outbox.high_water(session_id),
            )

    async def finish_release(session_id: uuid.UUID, lease_id: str) -> None:
        try:
            await send_message(
                LeaseRelease(session_id=session_id, lease_id=uuid.UUID(lease_id))
            )
        except Exception:
            pending_releases[session_id] = lease_id
            log.exception("lease release failed")
            return
        if pending_releases.get(session_id) == lease_id:
            del pending_releases[session_id]
        if session_id not in session_leases:
            dedupe.forget(session_id)
            outbox.release(session_id)

    async def release_lease(session_id: uuid.UUID) -> None:
        relay.forget(session_id)
        if session_id in stopping:
            return
        lease_id = session_leases.get(session_id)
        if lease_id is None:
            return
        await flush_outbox(session_id, lease_id, "lease.release")
        if session_leases.get(session_id) != lease_id:
            return
        session_leases.pop(session_id, None)
        await finish_release(session_id, lease_id)

    async def release_pending() -> None:
        for session_id, lease_id in list(pending_releases.items()):
            if session_id in session_leases:
                pending_releases.pop(session_id, None)
                continue
            await flush_outbox(
                session_id, lease_id, "lease.release", active=lambda: True
            )
            await finish_release(session_id, lease_id)

    execution.note_stopped = release_lease
    raw_worker = hello.get("worker_id")
    if emitter is not None:
        emitter.set_worker_id(str(raw_worker) if raw_worker else None)
    for session_id in _reconcile_hello(
        outbox, relay, session_leases, hello_sessions, dedupe
    ):
        schedule_teardown(session_id, True)
    _seed_reaper_ttl(execution, hello.get("ttl"))
    for session_id in revoked_sessions(hello):
        known = forget_revoked(
            session_id, session_leases=session_leases, relay=relay, dedupe=dedupe
        )
        schedule_teardown(session_id, known)
    relay.attach(send_json)
    execution.socket_open = True
    replayed = outbox.begin_connection()
    if replayed:
        if metrics is not None:
            metrics.observe_worker_replayed(replayed)
        log_event(
            log,
            logging.INFO,
            "worker replays unacked envelopes",
            event="worker.replay",
            envelopes=replayed,
            sessions=len(outbox.pending_sessions()),
            unclaimed_sessions=len(
                [sid for sid in outbox.pending_sessions() if sid not in claimed]
            ),
        )

    async def report_seen(session_ids: list[uuid.UUID]) -> None:
        if getattr(execution, "seen_hook", None) is not report_seen:
            return
        await send_message(SandboxSeenMessage(session_ids=session_ids))

    execution.seen_hook = report_seen
    if hasattr(execution, "search_sender") and FEATURE_SEARCH in api_features:
        execution.search_sender = send_json

    async def send_inventory() -> None:
        unleased = _unleased_session_dirs(
            settings, session_leases, getattr(execution, "pool", None)
        )
        await send_message(
            InventoryMessage(
                sessions=[
                    InventoryEntry(
                        session_id=session_id,
                        lease_id=uuid.UUID(lease_id),
                        last_seq=outbox.high_water(session_id),
                    )
                    for session_id, lease_id in session_leases.items()
                ]
                + [
                    InventoryEntry(session_id=session_id, last_seq=0)
                    for session_id in unleased
                ]
            )
        )

    async def send_heartbeat() -> None:
        await send_json(worker_heartbeat(settings, drain=draining.is_set()))

    async def heartbeat_loop() -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(heartbeat)
            now = time.monotonic()
            gap = now - last
            last = now
            if metrics is not None:
                metrics.observe_worker_heartbeat_gap(gap)
            if gap > lease_ttl / 2:
                warnings.warning(
                    "worker heartbeat late",
                    event="worker.heartbeat.late",
                    error_code="heartbeat_late",
                    source="worker",
                    gap_seconds=round(gap, 3),
                    lease_ttl_seconds=lease_ttl,
                )
            await send_heartbeat()

    async def inventory_loop() -> None:
        while True:
            await asyncio.sleep(INVENTORY_INTERVAL)
            await send_inventory()

    async def drain_loop() -> tuple[str, float | None]:
        nonlocal drain_deadline
        await draining.wait()
        if drain_deadline is None:
            drain_deadline = _start_drain(execution, metrics, wait)
        await send_heartbeat()
        waiting_logged = False
        while True:
            await execution.pool.kill_unheld(reason="drain")
            if drain_idle(execution.pool.live(), command_tasks):
                if not outbox.pending_sessions():
                    _finish_drain(outbox, "drained", execution)
                    return "drained", drain_deadline
                if not waiting_logged:
                    waiting_logged = True
                    log_event(
                        log,
                        logging.INFO,
                        "worker drain waits for the outbox to be acked",
                        event="worker.drain.waiting",
                        unacked_sessions=len(outbox.pending_sessions()),
                        unacked_envelopes=outbox.describe()["messages"],
                    )
            remaining = drain_deadline - time.monotonic()
            if remaining <= 0:
                await shutdown_harvest()
                _finish_drain(outbox, "drain_timeout", execution)
                return "drain_timeout", drain_deadline
            await asyncio.sleep(min(0.5, remaining))

    async def shutdown_harvest() -> None:
        """Kill what is left while the socket is open, so the harvest can upload."""
        try:
            await asyncio.wait_for(execution.pool.close(), SHUTDOWN_HARVEST_TIMEOUT)
        except Exception:
            log.exception("worker shutdown harvest failed")

    def ack_command(command: WorkerCommand) -> dict[str, Any]:
        return LeaseAck(id=command.command_id, lease_id=command.lease_id).to_wire()

    def report_command_failed(command: WorkerCommand) -> None:
        try:
            outbox.append(
                command.session_id,
                "error",
                WorkerErrorPayload(
                    code="internal", message=f"Worker command {command.op} failed"
                ),
            )
        except Exception:
            log.exception("worker command failure report failed")

    async def command_task(command: WorkerCommand, *, duplicate: bool) -> None:
        await settled(command.session_id)
        if duplicate:
            await send_json(ack_command(command))
            return
        _adopt_cursor(outbox, command)
        await send_json(ack_command(command))
        log_command(command)
        try:
            result = await dispatch_command(execution, command)
        except asyncio.CancelledError:
            raise
        except Exception:
            dedupe.discard(command.session_id, str(command.command_id))
            report_command_failed(command)
            raise
        if result == "rejected":
            await release_lease(command.session_id)

    async def stop_session_command(
        command: WorkerCommand, stop_session: uuid.UUID
    ) -> None:
        raw_lease = str(command.lease_id)
        ack_first = FEATURE_SESSION_STOPPED in api_features
        if ack_first:
            await send_json(ack_command(command))
        await settled(stop_session)
        stopping.add(stop_session)
        try:
            await dispatch_command(execution, command)
            dedupe.forget(stop_session)
            await flush_outbox(stop_session, raw_lease, "session.stop")
            if session_leases.get(stop_session) == raw_lease:
                session_leases.pop(stop_session, None)
            outbox.release(stop_session)
        except Exception:
            dedupe.discard(stop_session, str(command.command_id))
            report_command_failed(command)
            log.exception(
                "worker session stop failed",
                extra={"session_id": str(command.session_id)},
            )
            return
        finally:
            stopping.discard(stop_session)
        if not ack_first:
            await send_json(ack_command(command))

    def malformed(size: int) -> None:
        if metrics is not None:
            metrics.observe_worker_message("in", "unknown", size)
        warnings.warning(
            "worker message malformed",
            event="worker.message.invalid",
            error_code="invalid_message",
            reason="malformed",
            size=size,
        )

    def handle(message: dict[str, Any]) -> None:
        try:
            with collect_unknown_fields() as unknown:
                parsed = parse_api_message(message)
            if unknown:
                note_unknown_fields(unknown, metrics=metrics, side="worker")
        except ValidationError:
            warnings.warning(
                "worker message invalid",
                event="worker.message.invalid",
                error_code="invalid_message",
                reason="invalid",
                type=str(wire_type(message)),
            )
            return
        if isinstance(parsed, CumulativeAck):
            outbox.acked(parsed.session_id, parsed.last_seq)
            return
        if isinstance(parsed, ArtifactPresignReply):
            waiters = getattr(execution, "presign_waiters", None)
            if isinstance(waiters, dict):
                handle_presign_reply(waiters, message)
            return
        if isinstance(parsed, SearchReply):
            handle_search_reply = getattr(execution, "handle_search_reply", None)
            if callable(handle_search_reply):
                handle_search_reply(message)
            return
        if isinstance(parsed, InventoryReply):
            _seed_reaper_ttl(execution, message.get("ttl"))
            for session_id in revoked_sessions(message):
                known = forget_revoked(
                    session_id,
                    session_leases=session_leases,
                    relay=relay,
                    dedupe=dedupe,
                )
                schedule_teardown(session_id, known)
            return
        if isinstance(parsed, LeaseRevoke):
            known = forget_revoked(
                parsed.session_id,
                session_leases=session_leases,
                relay=relay,
                dedupe=dedupe,
            )
            schedule_teardown(parsed.session_id, known)
            return
        if parsed is None:
            note_unknown_type(
                "type", message.get("type"), metrics=metrics, side="worker"
            )
            return
        if not isinstance(parsed, WorkerCommand):
            return
        command = parsed
        session_id = command.session_id
        if command.op not in COMMAND_OPS:
            if metrics is not None:
                metrics.observe_worker_command_received("unknown", "unknown_op")
            note_unknown_type("op", command.op, metrics=metrics, side="worker")
            return
        if dedupe.duplicate(session_id, str(command.command_id)):
            if metrics is not None:
                metrics.observe_worker_command_received(command.op, "duplicate")
            spawn(command_task(command, duplicate=True), "worker_command", command=True)
            return
        if command.op != "turn.cancel" or session_id in session_leases:
            session_leases[session_id] = str(command.lease_id)
        if command.op == "session.stop":
            spawn(
                stop_session_command(command, session_id), "worker_stop", command=True
            )
            return
        spawn(command_task(command, duplicate=False), "worker_command", command=True)

    async def receive_loop() -> None:
        while True:
            incoming = await sock.recv()
            try:
                text = incoming if isinstance(incoming, str) else incoming.decode()
                message = json.loads(text)
            except ValueError:
                malformed(
                    wire_bytes(incoming) if isinstance(incoming, str) else len(incoming)
                )
                continue
            size = wire_bytes(text)
            if not isinstance(message, dict):
                malformed(size)
                continue
            if metrics is not None:
                metrics.observe_worker_message("in", wire_type(message), size)
            if log.isEnabledFor(logging.DEBUG):
                log.debug(
                    "worker message in",
                    extra={
                        "event": "worker.message",
                        "type": wire_type(message),
                        "size": size,
                    },
                )
            try:
                handle(message)
            except Exception:
                warnings.warning(
                    "worker message handler failed",
                    event="worker.message.failed",
                    error_code="message_failed",
                    exc_info=True,
                    type=str(wire_type(message)),
                )

    runners = {
        "recv": asyncio.create_task(receive_loop()),
        "pump": asyncio.create_task(_pump_outbox(outbox, send_json)),
        "heartbeat": asyncio.create_task(heartbeat_loop()),
        "inventory": asyncio.create_task(inventory_loop()),
        "drain": asyncio.create_task(drain_loop()),
    }
    if pending_releases:
        spawn(release_pending(), "worker_release")
    try:
        done, _pending = await asyncio.wait(
            runners.values(), return_when=asyncio.FIRST_COMPLETED
        )
        if runners["drain"] in done:
            outcome = runners["drain"].result()
            end_reason = outcome[0]
            return outcome
        for task in done:
            task.result()
        raise RuntimeError("worker connection loop ended")
    except BaseException as exc:
        end_reason = (
            "cancelled"
            if isinstance(exc, asyncio.CancelledError)
            else _reconnect_reason(exc)
        )
        raise
    finally:
        for task in runners.values():
            task.cancel()
        for task in runners.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        execution.socket_open = False
        if getattr(execution, "note_stopped", None) is release_lease:
            execution.note_stopped = None
        if getattr(execution, "seen_hook", None) is report_seen:
            execution.seen_hook = None
        if getattr(execution, "search_sender", None) is send_json:
            execution.search_sender = None
            fail_search_waiters = getattr(execution, "fail_search_waiters", None)
            if callable(fail_search_waiters):
                fail_search_waiters()
        presign_waiters = getattr(execution, "presign_waiters", None)
        if isinstance(presign_waiters, dict):
            fail_lost_presign_waiters(presign_waiters, outbox)
        relay.detach()
        if metrics is not None:
            metrics.set_worker_connected(False)
        log_event(
            log,
            logging.INFO,
            "worker disconnected",
            event="worker.disconnected",
            reason=end_reason,
            duration_s=round(time.monotonic() - opened, 3),
            unacked_envelopes=outbox.describe()["messages"],
            unacked_sessions=len(outbox.pending_sessions()),
            deltas_dropped=getattr(relay, "dropped", 0),
        )
        if context_token is not None:
            unbind_log_context(context_token)
