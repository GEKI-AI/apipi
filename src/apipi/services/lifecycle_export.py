import asyncio
import hashlib
import hmac
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx

from apipi.common.background import watch_task
from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics
from apipi.common.timefmt import utc_ts
from apipi.config import Settings
from apipi.services.usage_export import EventSink, load_custom_sinks

log = logging.getLogger("apipi")

SCHEMA_VERSION = 1
_WARN_EVERY = 30.0
_RETRYABLE = frozenset({408, 429})


def parse_ts(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def shape_user_id(user_id: str | None, mode: str, key: str | None) -> str | None:
    if user_id is None or mode == "omit":
        return None
    if mode == "hash":
        secret = key or ""
        return hmac.new(secret.encode(), user_id.encode(), hashlib.sha256).hexdigest()
    return user_id


def lifecycle_configured(settings: Settings) -> bool:
    if settings.lifecycle_export_url:
        return True
    return bool(settings.lifecycle_sinks.strip())


def run_mode_allowed(settings: Settings, run_mode: str | None) -> bool:
    raw = settings.lifecycle_run_modes.strip()
    if not raw:
        return True
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    return run_mode in allowed


def lifecycle_active(settings: Settings) -> bool:
    return lifecycle_configured(settings)


def backoff_seconds(attempt: int, cap: float) -> float:
    exp = min(attempt, 16)
    base = min(cap, 0.25 * (2**exp))
    return min(cap, base * (0.5 + random.random() * 0.5))


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


class LifecycleEmitter:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        self.settings = settings
        self.metrics = metrics
        self.boot_id = str(uuid.uuid4())
        self.worker_id: str | None = None
        self.active = lifecycle_active(settings)
        heartbeat = settings.lifecycle_heartbeat
        self.heartbeat_s = (
            heartbeat.total_seconds()
            if self.active and isinstance(heartbeat, timedelta)
            else None
        )
        self._seq = 0
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._busy = False
        self._last_warn = float("-inf")
        self._sinks: list[EventSink] = []
        if self.active:
            self._queue = asyncio.Queue(maxsize=settings.lifecycle_queue)
            self._sinks = load_custom_sinks(
                settings.lifecycle_sinks, "APIPI_LIFECYCLE_SINKS"
            )

    def set_worker_id(self, worker_id: str | None) -> None:
        self.worker_id = worker_id or None

    def start(self) -> None:
        if not self.active or self._task is not None or self._queue is None:
            return
        self._task = asyncio.create_task(self._sender(), name="lifecycle_sender")
        watch_task(self._task, "lifecycle_sender", metrics=self.metrics)

    def emit_start(self, fields: dict[str, Any], *, cause: str) -> int | None:
        if not self.active or not self._allowed(fields):
            return None
        seq = self._next_seq()
        event = self._envelope("session.live.start", seq, fields.get("run_mode"))
        event.update(self._identity(fields))
        event["cause"] = cause
        self._put(event)
        return seq

    def emit_stop(
        self, fields: dict[str, Any], *, reason: str, live_ms: int
    ) -> int | None:
        if not self.active or not self._allowed(fields):
            return None
        seq = self._next_seq()
        event = self._envelope("session.live.stop", seq, fields.get("run_mode"))
        event.update(self._identity(fields))
        event["reason"] = reason
        event["started_at"] = fields.get("started_at")
        event["live_ms"] = live_ms
        event["start_seq"] = fields.get("start_seq")
        self._put(event)
        return seq

    def emit_heartbeat(self, entries: list[dict[str, Any]]) -> int | None:
        if not self.active or self.heartbeat_s is None:
            return None
        seq = self._next_seq()
        event = self._envelope("session.live.heartbeat", seq, None)
        event["interval_s"] = _interval_s(self.heartbeat_s)
        event["live"] = [self._entry(item) for item in entries if self._allowed(item)]
        self._put(event)
        return seq

    async def flush(self, timeout: float | None = None) -> None:
        if not self.active or self._queue is None:
            return
        self.start()
        limit = (
            timeout
            if timeout is not None
            else self.settings.lifecycle_export_timeout.total_seconds()
        )
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if self._queue.empty() and not self._busy:
                return
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        await self.flush()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return

    def pending(self) -> list[dict[str, Any]]:
        queue = self._queue
        if queue is None:
            return []
        raw = getattr(queue, "_queue", ())
        return list(cast(list[dict[str, Any]], raw))

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _allowed(self, fields: dict[str, Any]) -> bool:
        return run_mode_allowed(self.settings, _text(fields.get("run_mode")))

    def _envelope(self, kind: str, seq: int, run_mode: str | None) -> dict[str, Any]:
        return {
            "type": kind,
            "schema_version": SCHEMA_VERSION,
            "event_id": f"{self.boot_id}:{seq}",
            "boot_id": self.boot_id,
            "seq": seq,
            "ts": utc_ts(),
            "worker_id": self.worker_id,
            "instance_id": self.settings.instance_id,
            "run_mode": run_mode,
        }

    def _identity(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "tenant_id": _text(fields.get("tenant_id")),
            "org_id": _text(fields.get("org_id")),
            "session_id": _text(fields.get("session_id")),
            "agent_id": _text(fields.get("agent_id")),
            "user_id": self._user(fields.get("user_id")),
            "key_id": _text(fields.get("key_id")),
            "environment_type": fields.get("environment_type"),
            "sandbox_size": fields.get("sandbox_size"),
            "sandbox_image": fields.get("sandbox_image"),
            "image_version": fields.get("image_version"),
            "image_digest": fields.get("image_digest"),
            "run_mode": fields.get("run_mode"),
        }

    def _entry(self, fields: dict[str, Any]) -> dict[str, Any]:
        body = self._identity(fields)
        body["session_id"] = _text(fields.get("session_id"))
        body["start_seq"] = fields.get("start_seq")
        body["started_at"] = fields.get("started_at")
        return body

    def _user(self, value: object) -> str | None:
        raw = value if isinstance(value, str) else None
        return shape_user_id(
            raw,
            self.settings.lifecycle_user_id,
            self.settings.lifecycle_user_id_key,
        )

    def _put(self, event: dict[str, Any]) -> None:
        queue = self._queue
        if queue is None:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._overflow(event)
        self._depth()

    def _overflow(self, event: dict[str, Any]) -> None:
        self._observe("overflow")
        now = time.monotonic()
        if now - self._last_warn < _WARN_EVERY:
            return
        self._last_warn = now
        session_id = event.get("session_id")
        if session_id is None:
            live = event.get("live")
            if isinstance(live, list) and live:
                first = live[0]
                if isinstance(first, dict):
                    session_id = first.get("session_id")
        log_event(
            log,
            logging.WARNING,
            "lifecycle export overflow",
            event="lifecycle.export.overflow",
            error_code="export_overflow",
            session_id=session_id,
            type=event.get("type"),
        )

    def _depth(self) -> None:
        metrics = self.metrics
        queue = self._queue
        if metrics is None or queue is None:
            return
        metrics.set_lifecycle_queue_depth(queue.qsize())

    def _observe(self, result: str, count: int = 1) -> None:
        metrics = self.metrics
        if metrics is None or count <= 0:
            return
        for _ in range(count):
            metrics.observe_lifecycle_export(result)

    async def _sender(self) -> None:
        queue = self._queue
        if queue is None:
            return
        headers = {"content-type": "application/json"}
        token = self.settings.lifecycle_export_token
        if token:
            headers["authorization"] = f"Bearer {token}"
        while True:
            batch = await self._take_batch(queue)
            if not batch:
                self._busy = False
                continue
            try:
                self._deliver_sinks(batch)
                if self.settings.lifecycle_export_url:
                    await self._post(headers, batch)
            finally:
                self._busy = False
                self._depth()

    async def _take_batch(
        self, queue: asyncio.Queue[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        first = await queue.get()
        self._busy = True
        batch = [first]
        limit = self.settings.lifecycle_batch
        deadline = time.monotonic() + self.settings.lifecycle_batch_wait.total_seconds()
        while len(batch) < limit:
            if not queue.empty():
                batch.append(queue.get_nowait())
                continue
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                batch.append(await asyncio.wait_for(queue.get(), remain))
            except TimeoutError:
                break
        return batch

    def _deliver_sinks(self, batch: list[dict[str, Any]]) -> None:
        for event in batch:
            for sink in self._sinks:
                try:
                    sink.emit(event)
                except Exception:
                    log_event(
                        log,
                        logging.WARNING,
                        "lifecycle sink failed",
                        event="lifecycle.export.dropped",
                        error_code="export_drop",
                        exc_info=True,
                        session_id=event.get("session_id"),
                        type=event.get("type"),
                    )

    async def _post(self, headers: dict[str, str], batch: list[dict[str, Any]]) -> None:
        url = self.settings.lifecycle_export_url
        if url is None:
            return
        body = {"events": batch}
        attempt = 0
        cap = self.settings.lifecycle_retry_max.total_seconds()
        timeout = self.settings.lifecycle_export_timeout.total_seconds()
        while True:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, json=body, headers=headers)
            except Exception:
                self._observe("retry")
                await asyncio.sleep(backoff_seconds(attempt, cap))
                attempt += 1
                continue
            code = response.status_code
            if 200 <= code < 300:
                self._observe("ok", len(batch))
                return
            if code in _RETRYABLE or code >= 500:
                self._observe("retry")
                await asyncio.sleep(backoff_seconds(attempt, cap))
                attempt += 1
                continue
            self._observe("drop", len(batch))
            log_event(
                log,
                logging.WARNING,
                "lifecycle export dropped",
                event="lifecycle.export.dropped",
                error_code="export_drop",
                session_id=batch[0].get("session_id") if batch else None,
                type=batch[0].get("type") if batch else None,
            )
            return


def _interval_s(seconds: float) -> int | float:
    if seconds == int(seconds):
        return int(seconds)
    return seconds


def create_lifecycle(
    settings: Settings, metrics: Metrics | None = None
) -> LifecycleEmitter | None:
    if not lifecycle_active(settings):
        return None
    return LifecycleEmitter(settings, metrics)


def heartbeat_fields(row: Any) -> dict[str, Any]:
    """Build a heartbeat identity entry for one leased session row."""
    environment = row.environment if isinstance(row.environment, dict) else {}
    return {
        "tenant_id": _text(row.tenant_id),
        "org_id": _text(row.org_id),
        "session_id": _text(row.id),
        "agent_id": _text(row.agent_id),
        "user_id": _text(row.user_id),
        "key_id": _text(row.key_id),
        "environment_type": environment.get("type"),
        "sandbox_size": row.sandbox_size or environment.get("sandbox_size"),
        "sandbox_image": row.sandbox_image or environment.get("sandbox_image"),
        "image_version": row.sandbox_image_version,
        "image_digest": None,
        "run_mode": None,
        "started_at": None,
        "start_seq": None,
    }


async def api_heartbeat_loop(
    settings: Settings,
    emitter: Any,
    hub: Any,
    store: Any,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_emits: int | None = None,
) -> None:
    """Derive lifecycle heartbeats on the API from worker live sets.

    Each replica exports the sessions its own workers report in their
    periodic inventory; the downstream `reconcile` joins boots, so
    per-replica heartbeats stay correct."""
    interval = emitter.heartbeat_s
    if interval is None or interval <= 0:
        return
    emitted = 0
    next_at = clock() + interval
    while max_emits is None or emitted < max_emits:
        wait = max(0.0, next_at - clock())
        await sleep(wait)
        now = clock()
        if now + 1e-9 < next_at:
            continue
        live: list[dict[str, Any]] = []
        try:
            session_ids = hub.known_live_sessions()
            async with store.session() as db:
                from apipi.store.repo import get_session_by_id

                for session_id in session_ids:
                    row = await get_session_by_id(db, session_id)
                    if row is None or row.lease_id is None:
                        continue
                    entry = heartbeat_fields(row)
                    conn = hub.get(row.worker_id) if row.worker_id is not None else None
                    entry["run_mode"] = conn.run_mode if conn is not None else None
                    live.append(entry)
        except Exception:
            log.exception("lifecycle heartbeat failed")
        emitter.emit_heartbeat(live)
        emitted += 1
        next_at = now + interval


def reconcile(
    events: list[dict[str, Any]],
    *,
    now: datetime,
    grace_s: float = 0.0,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    ordered: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen:
                continue
            seen.add(event_id)
        ordered.append(event)
    by_boot: dict[str, list[dict[str, Any]]] = {}
    boot_order: list[str] = []
    for event in ordered:
        boot_id = event.get("boot_id")
        if not isinstance(boot_id, str):
            continue
        if boot_id not in by_boot:
            by_boot[boot_id] = []
            boot_order.append(boot_id)
        by_boot[boot_id].append(event)
    opens: dict[tuple[str, int], dict[str, Any]] = {}
    last_hb: dict[str, datetime] = {}
    last_event: dict[str, datetime] = {}
    intervals: dict[str, float] = {}
    identity: dict[str, tuple[str | None, str | None]] = {}
    closed: list[dict[str, Any]] = []

    def close_one(
        item: dict[str, Any], *, at: datetime, reason: str, live_ms: int | None
    ) -> None:
        closed.append(
            {
                "boot_id": item["boot_id"],
                "session_id": item["session_id"],
                "start_seq": item["start_seq"],
                "started_at": item["started_at"],
                "closed_at": utc_ts(at),
                "reason": reason,
                "live_ms": live_ms,
            }
        )

    for boot_id in boot_order:
        rows = sorted(by_boot[boot_id], key=lambda item: int(item.get("seq") or 0))
        for event in rows:
            moment = event.get("ts")
            if isinstance(moment, str):
                last_event[boot_id] = parse_ts(moment)
            worker = event.get("worker_id")
            instance = event.get("instance_id")
            previous_id = identity.get(boot_id, (None, None))
            identity[boot_id] = (
                worker if isinstance(worker, str) else previous_id[0],
                instance if isinstance(instance, str) else previous_id[1],
            )
            kind = event.get("type")
            if kind == "session.live.start":
                seq = int(event["seq"])
                opens[(boot_id, seq)] = {
                    "boot_id": boot_id,
                    "session_id": event.get("session_id"),
                    "start_seq": seq,
                    "started_at": event.get("ts"),
                    "last_listed": None,
                }
            elif kind == "session.live.stop":
                key = (boot_id, int(event.get("start_seq") or 0))
                item = opens.pop(key, None)
                if item is None or not isinstance(moment, str):
                    continue
                raw_ms = event.get("live_ms")
                live_ms = raw_ms if isinstance(raw_ms, int) else None
                close_one(
                    item,
                    at=parse_ts(moment),
                    reason=str(event.get("reason") or "stop"),
                    live_ms=live_ms,
                )
            elif kind == "session.live.heartbeat" and isinstance(moment, str):
                listed_at = parse_ts(moment)
                raw_interval = event.get("interval_s")
                if isinstance(raw_interval, (int, float)):
                    intervals[boot_id] = float(raw_interval)
                listed: set[tuple[str, int]] = set()
                live = event.get("live")
                if isinstance(live, list):
                    for entry in live:
                        if not isinstance(entry, dict):
                            continue
                        session_id = entry.get("session_id")
                        start_seq = entry.get("start_seq")
                        if not isinstance(session_id, str) or not isinstance(
                            start_seq, int
                        ):
                            continue
                        listed.add((session_id, start_seq))
                        key = (boot_id, start_seq)
                        if key not in opens:
                            opens[key] = {
                                "boot_id": boot_id,
                                "session_id": session_id,
                                "start_seq": start_seq,
                                "started_at": entry.get("started_at"),
                                "last_listed": listed_at,
                            }
                        else:
                            opens[key]["last_listed"] = listed_at
                for key, item in list(opens.items()):
                    if key[0] != boot_id:
                        continue
                    mark = (item["session_id"], item["start_seq"])
                    if mark in listed:
                        continue
                    at = item["last_listed"] or last_event.get(boot_id) or listed_at
                    opens.pop(key, None)
                    close_one(item, at=at, reason="lost", live_ms=None)
                last_hb[boot_id] = listed_at
    seen_worker: dict[str, str] = {}
    seen_instance: dict[str, str] = {}
    for boot_id in boot_order:
        worker, instance = identity.get(boot_id, (None, None))
        previous: list[str] = []
        if worker and worker in seen_worker and seen_worker[worker] != boot_id:
            previous.append(seen_worker[worker])
        same_instance = instance in seen_instance and seen_instance[instance] != boot_id
        if instance and same_instance:
            previous.append(seen_instance[instance])
        for old in previous:
            at = last_hb.get(old) or last_event.get(old)
            if at is None:
                continue
            for key, item in list(opens.items()):
                if key[0] != old:
                    continue
                opens.pop(key, None)
                close_one(item, at=at, reason="worker_lost", live_ms=None)
        if worker:
            seen_worker[worker] = boot_id
        if instance:
            seen_instance[instance] = boot_id
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    for boot_id, gap in intervals.items():
        seen_at = last_hb.get(boot_id)
        if seen_at is None:
            continue
        if (now - seen_at).total_seconds() <= (2 * gap) + grace_s:
            continue
        for key, item in list(opens.items()):
            if key[0] != boot_id:
                continue
            opens.pop(key, None)
            close_one(item, at=seen_at, reason="worker_lost", live_ms=None)
    return closed
