import asyncio
import contextlib
import json
import logging
import os
import signal
import time
import uuid
from typing import Any
from urllib.parse import urlparse, urlunparse

import websockets

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
)
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


def answer_store_check(settings: Any, hello: dict[str, Any]) -> dict[str, str] | None:
    """Build the `store.proof` reply for a hello challenge, if any.

    Raises ConfigError with the documented shared-path message when the
    worker cannot read the marker back.
    """
    from apipi.common.dirs import store_root
    from apipi.common.store_check import SHARED_STORE_ERROR, read_store_check
    from apipi.config import ConfigError

    raw = hello.get("store_check")
    if not isinstance(raw, dict):
        return None
    marker = raw.get("marker")
    nonce = raw.get("nonce")
    if not isinstance(marker, str) or not isinstance(nonce, str):
        return None
    if not read_store_check(store_root(settings), marker, nonce):
        raise ConfigError(SHARED_STORE_ERROR)
    return {"type": "store.proof", "marker": marker, "nonce": nonce}


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

    payload: dict[str, object] = {
        "type": "heartbeat",
        "capacity": settings.max_sessions,
        "memory_mb": settings.node_memory_mb(),
        "run_mode": settings.run_mode,
        "accepts": sorted(resolved_worker_accepts(settings)),
        "arch": worker_arch(),
        "image_store_version": settings.image_store_version or "",
        "images": _heartbeat_images(settings),
    }
    if drain:
        payload["drain"] = True
    return payload


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
) -> list[dict[str, Any]]:
    claimed = []
    for session_id, lease_id in session_leases.items():
        claimed.append(
            {
                "session_id": str(session_id),
                "lease_id": lease_id,
                "last_seq": outbox.high_water(session_id),
            }
        )
    return claimed


async def _reconcile_hello(
    execution: Any,
    outbox: "Outbox",
    relay: Any,
    session_leases: dict[uuid.UUID, str],
    sessions: dict[str, Any],
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


def _hello_seconds(hello: dict[str, Any], key: str) -> float:
    value = hello.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"worker register failed: hello has no valid {key}")
    return float(value)


def _adopt_cursor(outbox: "Outbox", message: dict[str, Any]) -> None:
    """Continue the session sequence from the cursor the API put in a command."""
    if message.get("op") not in CURSOR_OPS:
        return
    payload = message.get("payload")
    raw = payload.get("last_seq") if isinstance(payload, dict) else None
    try:
        session_id = uuid.UUID(str(message.get("session_id")))
    except (ValueError, TypeError):
        return
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        log_event(
            log,
            logging.WARNING,
            "worker command has no sequence cursor",
            event="worker.command.cursor_missing",
            session_id=session_id,
            op=message.get("op"),
        )
        return
    outbox.set_base(session_id, raw)


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
    from apipi.protocol import PROTOCOL_VERSION
    from apipi.worker.accepts import resolved_worker_accepts

    send_lock = asyncio.Lock()
    metrics = getattr(execution, "metrics", None)

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await sock.send(json.dumps(payload))

    await send_json(
        {
            "type": "register",
            "protocol": PROTOCOL_VERSION,
            "capabilities": {},
            "accepts": sorted(resolved_worker_accepts(settings)),
            "running": _running_claim(session_leases, outbox),
            "capacity": settings.max_sessions,
            "memory_mb": settings.node_memory_mb(),
            "run_mode": settings.run_mode,
            "arch": worker_arch(),
            "images": _heartbeat_images(settings),
        }
    )
    raw = await sock.recv()
    hello = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
    if not isinstance(hello, dict) or not hello.get("ok"):
        error = hello.get("error") if isinstance(hello, dict) else "unauthorized"
        raise ConfigError(f"worker register failed: {error}")
    heartbeat = _hello_seconds(hello, "heartbeat_seconds")
    lease_ttl = _hello_seconds(hello, "lease_ttl_seconds")
    proof = answer_store_check(settings, hello if isinstance(hello, dict) else {})
    if proof is not None:
        await send_json(proof)
    sessions = hello.get("sessions")
    hello_sessions = sessions if isinstance(sessions, dict) else {}
    log.info(
        "worker hello",
        extra={
            "worker_id": hello.get("worker_id"),
            "sessions": hello_sessions,
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
            await send_json(
                {
                    "type": "lease.release",
                    "session_id": str(session_id),
                    "lease_id": lease_id,
                }
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
        await send_json(
            {
                "type": "sandbox.seen",
                "session_ids": [str(session_id) for session_id in session_ids],
            }
        )

    execution.seen_hook = report_seen
    if hasattr(execution, "search_sender"):
        execution.search_sender = send_json

    async def send_inventory() -> None:
        unleased = _unleased_session_dirs(
            settings, session_leases, getattr(execution, "pool", None)
        )
        await send_json(
            {
                "type": "inventory",
                "sessions": [
                    {
                        "session_id": str(session_id),
                        "lease_id": lease_id,
                        "last_seq": outbox.high_water(session_id),
                    }
                    for session_id, lease_id in session_leases.items()
                ]
                + [
                    {"session_id": str(session_id), "last_seq": 0}
                    for session_id in unleased
                ],
            }
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

    def ack_command(message: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "lease.ack",
            "id": message.get("id"),
            "lease_id": message.get("lease_id"),
        }

    async def stop_session_command(
        message: dict[str, Any], stop_session: uuid.UUID | None
    ) -> None:
        raw_lease = message.get("lease_id")
        if stop_session is not None:
            stopping.add(stop_session)
        try:
            await dispatch_command(execution, message)
            if stop_session is not None:
                dedupe.forget(stop_session)
                if isinstance(raw_lease, str):
                    await flush_outbox(stop_session, raw_lease, "session.stop")
                if session_leases.get(stop_session) == raw_lease:
                    session_leases.pop(stop_session, None)
        except Exception:
            log.exception(
                "worker session stop failed",
                extra={"session_id": str(message.get("session_id"))},
            )
            return
        finally:
            if stop_session is not None:
                stopping.discard(stop_session)
        await send_json(ack_command(message))

    async def receive_loop() -> None:
        while True:
            incoming = await sock.recv()
            text = incoming if isinstance(incoming, str) else incoming.decode()
            message = json.loads(text)
            if not isinstance(message, dict):
                continue
            if message.get("type") == "ack":
                raw_session = message.get("session_id")
                last_seq = message.get("last_seq")
                try:
                    ack_session = uuid.UUID(str(raw_session))
                    ack_seq = int(last_seq)  # type: ignore[arg-type]
                except (ValueError, TypeError):
                    continue
                outbox.acked(ack_session, ack_seq)
                continue
            if message.get("type") == "artifact.presign.reply":
                from apipi.worker.artifact_upload import handle_presign_reply

                waiters = getattr(execution, "presign_waiters", None)
                if isinstance(waiters, dict):
                    handle_presign_reply(waiters, message)
                continue
            if message.get("type") == "search.reply":
                handle_search_reply = getattr(execution, "handle_search_reply", None)
                if callable(handle_search_reply):
                    handle_search_reply(message)
                continue
            if message.get("type") == "inventory.reply":
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
            if message.get("type") == "command":
                raw_lease = message.get("lease_id")
                raw_session = message.get("session_id")
                if isinstance(raw_lease, str) and isinstance(raw_session, str):
                    session_leases[uuid.UUID(raw_session)] = raw_lease
                _adopt_cursor(outbox, message)
            if message.get("type") == "command" and message.get("op") == "session.stop":
                command_id = message.get("id")
                try:
                    stop_session = uuid.UUID(str(message.get("session_id")))
                except (ValueError, TypeError):
                    stop_session = None
                if (
                    stop_session is not None
                    and isinstance(command_id, str)
                    and dedupe.duplicate(stop_session, command_id)
                ):
                    await send_json(ack_command(message))
                    continue
                task = asyncio.create_task(stop_session_command(message, stop_session))
                command_tasks.add(task)
                task.add_done_callback(command_tasks.discard)
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                continue
            if message.get("type") == "lease.revoke":
                revoked = message.get("session_id")
                if isinstance(revoked, str):
                    try:
                        revoked_id = uuid.UUID(revoked)
                    except ValueError:
                        continue
                    dedupe.forget(revoked_id)
                    known = revoked_id in session_leases
                    session_leases.pop(revoked_id, None)
                    relay.forget(revoked_id)
                    await execution.teardown(revoked_id)
                    outbox.drop_session(revoked_id)
                    if not known:
                        await wipe_unknown_workspace(
                            settings, execution, outbox, revoked_id
                        )
                continue
            if message.get("type") == "command":
                command_id = message.get("id")
                try:
                    command_session = uuid.UUID(str(message.get("session_id")))
                except (ValueError, TypeError):
                    command_session = None
                if (
                    command_session is not None
                    and isinstance(command_id, str)
                    and dedupe.duplicate(command_session, command_id)
                ):
                    await send_json(ack_command(message))
                    continue
                await send_json(ack_command(message))
                log.info(
                    "worker command",
                    extra={
                        "op": message.get("op"),
                        "session_id": message.get("session_id"),
                        **command_log_context(message),
                    },
                )
                task = asyncio.create_task(dispatch_command(execution, message))
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
