import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets
from pydantic import ValidationError

from apipi.common.logutil import log_event
from apipi.config import (
    ConfigError,
    Settings,
    load_worker_token,
    reject_legacy_worker_token,
    reject_worker_database_url,
)
from apipi.protocol import (
    CURSOR_OPS,
    PROTOCOL_VERSION,
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
    WorkerImageInfo,
    parse_api_message,
)
from apipi.worker.artifact_upload import handle_presign_reply
from apipi.worker.commands import CommandDedupe, command_log_context, dispatch_command
from apipi.worker.deltas import DeltaRelay, LiveRedirectBus
from apipi.worker.inventory import (
    _apply_inventory_reply,
    _unleased_session_dirs,
    wipe_unknown_workspace,
)
from apipi.worker.outbox import Outbox

log = logging.getLogger("apipi.worker")

INVENTORY_INTERVAL = 60.0
RELEASE_FLUSH_TIMEOUT = 10.0


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
    execution = local_execution(
        settings,
        hub=LiveRedirectBus(bus, relay),
        metrics=metrics,
        tracing=tracing,
        outbox=outbox,
    )
    # Split workers report lifecycle over the socket; the API owns the
    # export. The reporter only appends durable envelopes, and the
    # reaper learns idle TTLs from inventory replies, so no background
    # loop needs the database.
    execution.pool.lifecycle = OutboxLifecycleReporter(outbox)
    await bus.start()
    tasks: set[asyncio.Task[None]] = set()
    if metrics is not None:
        from apipi.worker.scrape import serve_metrics

        tasks.add(
            asyncio.create_task(
                serve_metrics(
                    metrics,
                    host=settings.worker_metrics_host,
                    port=settings.worker_metrics_port,
                )
            )
        )
        log.info(
            "worker metrics",
            extra={
                "host": settings.worker_metrics_host,
                "port": settings.worker_metrics_port,
            },
        )
    tasks.add(asyncio.create_task(execution.observe_loop()))
    tasks.add(asyncio.create_task(execution.reap_loop()))
    tasks.add(asyncio.create_task(execution.reap_workspace_loop()))
    tasks.add(asyncio.create_task(execution.sandbox_seen_loop()))
    emitter = getattr(getattr(execution, "pool", None), "lifecycle", None)
    if emitter is not None:
        emitter.start()
    log.info("worker connect", extra={"url": ws_url})
    spooled = outbox.load_spool()
    if spooled:
        log.info(
            "worker outbox spool loaded",
            extra={"sessions": len(spooled)},
        )
    draining = asyncio.Event()
    _install_drain_signals(draining)
    drain_deadline: float | None = None
    wait = drain_timeout_seconds(settings, drain_timeout)
    command_tasks: set[asyncio.Task[None]] = set()
    session_leases: dict[uuid.UUID, str] = {}
    # Command dedupe is worker-lifetime, not per-connection: the API
    # replays unacked commands after a reconnect with the same
    # `command_id`, and only a dedupe that survives the socket tells
    # the replay from a new turn. Entries are forgotten when the
    # session is torn down, stopped, or revoked.
    dedupe = CommandDedupe()
    status = 0
    backoff = 0.5
    connect_kwargs = _worker_connect_kwargs(settings, ws_url)
    connect_factory = connect if connect is not None else websockets.connect
    try:
        while True:
            try:
                async with connect_factory(
                    ws_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
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
                    )
                backoff = 0.5
                if outcome == "drained":
                    break
                if outcome == "drain_timeout":
                    status = 1
                    break
            except ConfigError:
                raise
            except Exception:
                log.warning("worker connection lost, reconnecting", exc_info=True)
                if draining.is_set() and drain_idle(
                    execution.pool.live(), command_tasks
                ):
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 5.0)
                continue
    finally:
        relay.detach()
        for task in tasks:
            task.cancel()
        await execution.close()
        if execution.tracing is not None:
            execution.tracing.shutdown()
        await bus.close()
    return status


def worker_outbox(settings: Settings) -> "Outbox":
    """Build the worker outbox from settings."""
    from apipi.worker.outbox import Outbox

    return Outbox(
        max_messages=settings.worker_outbox_max_messages,
        max_bytes=settings.worker_outbox_max_bytes,
        spool_dir=settings.worker_outbox_dir,
    )


async def _pump_outbox(outbox: "Outbox", send: Any) -> None:
    """Send buffered durable envelopes until cancelled (one writer)."""
    while True:
        await outbox.wait_dirty()
        for session_id in outbox.pending_sessions():
            for envelope in outbox.pending(session_id):
                await send(envelope)


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


async def _reconcile_hello(
    execution: Any,
    outbox: "Outbox",
    relay: Any,
    session_leases: dict[uuid.UUID, str],
    sessions: Mapping[Any, Any],
    hello: dict[str, Any] | None = None,
    dedupe: "CommandDedupe | None" = None,
    settings: Any | None = None,
) -> None:
    """Adopt the API cursors; drop turns the API no longer leases."""
    kept: set[uuid.UUID] = set()
    for raw_id, last_seq in sessions.items():
        try:
            session_id = uuid.UUID(str(raw_id))
            cursor = int(last_seq)
        except (ValueError, TypeError):
            continue
        kept.add(session_id)
        outbox.set_base(session_id, max(cursor, 0))
    for session_id in [sid for sid in session_leases if sid not in kept]:
        session_leases.pop(session_id, None)
        relay.forget(session_id)
        if dedupe is not None:
            dedupe.forget(session_id)
        await execution.teardown(session_id)
        outbox.drop_session(session_id)
        log.info(
            "worker dropped unleased session", extra={"session_id": str(session_id)}
        )
    for session_id in outbox.pending_sessions():
        if session_id not in kept and session_id not in session_leases:
            outbox.drop_session(session_id)
    if hello is not None:
        await _apply_inventory_reply(
            execution,
            hello,
            session_leases=session_leases,
            outbox=outbox,
            relay=relay,
            dedupe=dedupe,
            settings=settings,
        )
    outbox.mark_dirty()


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
) -> tuple[str, float | None]:
    """Serve one socket; returns drained or drain_timeout (loss raises).

    `dedupe` is worker-lifetime (owned by `run_worker`): reconnects
    replay unacked commands with the same `command_id`, so only a
    dedupe that survives the socket suppresses the second dispatch.

    Heartbeats, the inventory, and the drain check run on their own
    timers, so a socket that never goes quiet cannot starve them. The
    heartbeat interval and the lease TTL come from the API in `hello`.
    """
    from apipi.worker.accepts import resolved_worker_accepts

    send_lock = asyncio.Lock()
    metrics = getattr(execution, "metrics", None)

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await sock.send(json.dumps(payload))

    async def send_message(message: WireModel) -> None:
        await send_json(message.to_wire())

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
            images=[
                WorkerImageInfo.model_validate(item)
                for item in _heartbeat_images(settings)
            ],
        )
    )
    raw = await sock.recv()
    hello = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
    if not isinstance(hello, dict) or not hello.get("ok"):
        error = hello.get("error") if isinstance(hello, dict) else "unauthorized"
        raise ConfigError(f"worker register failed: {error}")
    try:
        welcome = HelloReply.model_validate(hello)
    except ValidationError as exc:
        raise ConfigError(f"worker register failed: invalid hello: {exc}") from exc
    heartbeat = welcome.heartbeat_seconds
    lease_ttl = welcome.lease_ttl_seconds
    proof = answer_store_check(settings, hello)
    if proof is not None:
        await send_json(proof)
    hello_sessions = welcome.sessions
    log.info(
        "worker hello",
        extra={
            "worker_id": hello.get("worker_id"),
            "sessions": {str(key): value for key, value in hello_sessions.items()},
            "lease_ttl_seconds": lease_ttl,
            "heartbeat_seconds": heartbeat,
        },
    )
    stopping: set[uuid.UUID] = set()

    async def flush_outbox(session_id: uuid.UUID, lease_id: str, action: str) -> None:
        flushed = await _outbox_flushed(
            outbox,
            session_id,
            timeout=RELEASE_FLUSH_TIMEOUT,
            active=lambda: session_leases.get(session_id) == lease_id,
        )
        if not flushed:
            log_event(
                log,
                logging.WARNING,
                "worker outbox not acked before release",
                event="worker.release.unflushed",
                session_id=session_id,
                action=action,
                acked_seq=outbox.acked_seq(session_id),
                high_water=outbox.high_water(session_id),
            )

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
        try:
            await send_message(
                LeaseRelease(session_id=session_id, lease_id=uuid.UUID(lease_id))
            )
        except Exception:
            log.exception("lease release failed")

    execution.note_stopped = release_lease
    raw_worker = hello.get("worker_id")
    if emitter is not None:
        emitter.set_worker_id(str(raw_worker) if raw_worker else None)
    await _reconcile_hello(
        execution,
        outbox,
        relay,
        session_leases,
        hello_sessions,
        hello,
        dedupe=dedupe,
        settings=settings,
    )
    relay.attach(send_json)
    pump = asyncio.create_task(_pump_outbox(outbox, send_json))

    async def report_seen(session_ids: list[uuid.UUID]) -> None:
        if getattr(execution, "seen_hook", None) is not report_seen:
            return
        await send_message(SandboxSeenMessage(session_ids=session_ids))

    execution.seen_hook = report_seen
    if hasattr(execution, "search_sender"):
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
                log_event(
                    log,
                    logging.WARNING,
                    "worker heartbeat late",
                    event="worker.heartbeat.late",
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
            drain_deadline = time.monotonic() + wait
            log.info("worker drain")
            await send_heartbeat()
        while True:
            await execution.pool.kill_unheld(reason="drain")
            if drain_idle(execution.pool.live(), command_tasks):
                return "drained", drain_deadline
            if time.monotonic() >= drain_deadline:
                return "drain_timeout", drain_deadline
            await asyncio.sleep(0.5)

    def ack_command(command: WorkerCommand) -> dict[str, Any]:
        return LeaseAck(id=command.command_id, lease_id=command.lease_id).to_wire()

    async def stop_session_command(
        command: WorkerCommand, stop_session: uuid.UUID
    ) -> None:
        raw_lease = str(command.lease_id)
        stopping.add(stop_session)
        try:
            await dispatch_command(execution, command)
            dedupe.forget(stop_session)
            await flush_outbox(stop_session, raw_lease, "session.stop")
            if session_leases.get(stop_session) == raw_lease:
                session_leases.pop(stop_session, None)
        except Exception:
            log.exception(
                "worker session stop failed",
                extra={"session_id": str(command.session_id)},
            )
            return
        finally:
            stopping.discard(stop_session)
        await send_json(ack_command(command))

    async def receive_loop() -> None:
        while True:
            incoming = await sock.recv()
            text = incoming if isinstance(incoming, str) else incoming.decode()
            message = json.loads(text)
            if not isinstance(message, dict):
                continue
            try:
                parsed = parse_api_message(message)
            except ValidationError:
                log.warning(
                    "worker message invalid",
                    extra={
                        "event": "worker.message.invalid",
                        "type": str(message.get("type")),
                    },
                )
                continue
            if isinstance(parsed, CumulativeAck):
                outbox.acked(parsed.session_id, parsed.last_seq)
                continue
            if isinstance(parsed, ArtifactPresignReply):
                waiters = getattr(execution, "presign_waiters", None)
                if isinstance(waiters, dict):
                    handle_presign_reply(waiters, message)
                continue
            if isinstance(parsed, SearchReply):
                handle_search_reply = getattr(execution, "handle_search_reply", None)
                if callable(handle_search_reply):
                    handle_search_reply(message)
                continue
            if isinstance(parsed, InventoryReply):
                await _apply_inventory_reply(
                    execution,
                    message,
                    session_leases=session_leases,
                    outbox=outbox,
                    relay=relay,
                    dedupe=dedupe,
                    settings=settings,
                )
                continue
            if isinstance(parsed, LeaseRevoke):
                dedupe.forget(parsed.session_id)
                known = parsed.session_id in session_leases
                session_leases.pop(parsed.session_id, None)
                relay.forget(parsed.session_id)
                await execution.teardown(parsed.session_id)
                outbox.drop_session(parsed.session_id)
                if not known:
                    await wipe_unknown_workspace(
                        settings, execution, outbox, parsed.session_id
                    )
                continue
            if not isinstance(parsed, WorkerCommand):
                continue
            command = parsed
            command_id = str(command.command_id)
            session_leases[command.session_id] = str(command.lease_id)
            _adopt_cursor(outbox, command)
            if command.op == "session.stop":
                if dedupe.duplicate(command.session_id, command_id):
                    await send_json(ack_command(command))
                    continue
                task = asyncio.create_task(
                    stop_session_command(command, command.session_id)
                )
                command_tasks.add(task)
                task.add_done_callback(command_tasks.discard)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                continue
            if dedupe.duplicate(command.session_id, command_id):
                await send_json(ack_command(command))
                continue
            await send_json(ack_command(command))
            log.info(
                "worker command",
                extra={
                    "op": command.op,
                    "session_id": str(command.session_id),
                    **command_log_context(command),
                },
            )
            task = asyncio.create_task(dispatch_command(execution, command))
            command_tasks.add(task)
            task.add_done_callback(command_tasks.discard)
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    runners = {
        "recv": asyncio.create_task(receive_loop()),
        "heartbeat": asyncio.create_task(heartbeat_loop()),
        "inventory": asyncio.create_task(inventory_loop()),
        "drain": asyncio.create_task(drain_loop()),
    }
    try:
        done, _pending = await asyncio.wait(
            runners.values(), return_when=asyncio.FIRST_COMPLETED
        )
        if runners["drain"] in done:
            return runners["drain"].result()
        for task in done:
            task.result()
        raise RuntimeError("worker connection loop ended")
    finally:
        for task in runners.values():
            task.cancel()
        for task in runners.values():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if getattr(execution, "seen_hook", None) is report_seen:
            execution.seen_hook = None
        if getattr(execution, "search_sender", None) is send_json:
            execution.search_sender = None
            fail_search_waiters = getattr(execution, "fail_search_waiters", None)
            if callable(fail_search_waiters):
                fail_search_waiters()
        relay.detach()
        pump.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump
