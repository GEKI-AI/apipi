"""EventBus: fan-out for session events across API replicas.

A single process uses :class:`InMemoryEventBus` (the previous ``EventHub``
semantics: per-session queues in this process). A shared Postgres store
uses :class:`PostgresEventBus`: wakes are sent with ``NOTIFY`` after the
storing transaction commits, and each replica holds one dedicated
``LISTEN`` connection (outside the SQLAlchemy pool) that dispatches to
local subscribers. The interface leaves room for NATS or Redis later.

Message kinds:

* ``wake`` (``session_id``, ``seq``): a stored event was committed. The
  receiver re-reads ``after_seq`` from the database, so a lost
  notification is recovered by the fallback poll.
* ``live`` (delta batch): ephemeral deltas, never stored. At-most-once;
  the final item is the source of truth.
* ``forward`` and ``forward_result`` (a mailbox row id and a status):
  sent to one replica on its own channel, see ``workerhub/forward.py``.
  The body of a forwarded command never travels here.
* A full stored-event body (it carries ``seq``) is delivered to
  subscribers in the publishing process directly, with no extra read.
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from typing import Any

import asyncpg

from apipi.common.background import run_loop
from apipi.common.event_bus import (
    EventBus,
    InMemoryEventBus,
    InstanceHandler,
    LocalFanout,
    is_wake,
    message_seq,
    wake_message,
)
from apipi.common.logutil import RateLimitedLog
from apipi.config import ConfigError, Settings, is_sqlite_url, postgres_url

log = logging.getLogger("apipi")

EVENT_CHANNEL = "apipi_events"
INSTANCE_CHANNEL_PREFIX = "apipi_fwd_"
NOTIFY_LIMIT = 8000
NOTIFY_TIMEOUT = 5.0
LIVE_HEADROOM = 256
_CONNECT_BACKOFF = 5.0
LIVE_WINDOW = 0.04
_RECONNECT_BACKOFF = (0.5, 1.0, 2.0, 5.0)
_QUEUE_SAMPLE_INTERVAL = 30.0


def instance_channel(instance_id: str) -> str:
    """The NOTIFY channel of one replica. Channel names stop at 63 bytes."""
    digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:24]
    return f"{INSTANCE_CHANNEL_PREFIX}{digest}"


def split_notify_batches(
    items: list[dict[str, Any]], *, limit: int = NOTIFY_LIMIT
) -> list[list[dict[str, Any]]]:
    """Split items so each batch encodes to at most ``limit`` bytes.

    A single item over the limit goes alone; NOTIFY cannot carry more
    than 8000 bytes and such a delta is already too big to split here.
    """
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 2
    for item in items:
        size = len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
        if current and current_size + 1 + size > limit:
            batches.append(current)
            current = []
            current_size = 2
        current.append(item)
        current_size += size + 1
    if current:
        batches.append(current)
    return batches


class PostgresEventBus(LocalFanout):
    """Fan-out over Postgres LISTEN/NOTIFY.

    The publishing process dispatches the full message locally and sends
    a small ``wake`` (or a coalesced ``live`` batch) to other replicas.
    Every payload carries the instance origin id, and the listener
    skips its own payloads: Postgres delivers a NOTIFY to every
    listener, including the sender's, and the local copy was already
    dispatched.
    ``NOTIFY`` is only sent after the storing commit (callers publish
    from an after-commit hook), the ``LISTEN`` connection is dedicated
    and outside the SQLAlchemy pool, and the fallback poll covers the
    gap while a dropped listener reconnects.

    Messages for one replica (`send_instance`) use a channel of their own
    per instance, so a replica only wakes for what is addressed to it.
    """

    forwards = True

    def __init__(self, dsn: str, *, metrics: Any | None = None) -> None:
        super().__init__()
        self._dsn = dsn
        self._metrics = metrics
        self._origin = uuid.uuid4().hex
        self._listen: asyncpg.Connection | None = None
        self._publish_conn: asyncpg.Connection | None = None
        self._live: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self._running = False
        self._lock = asyncio.Lock()
        self._notify_lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._connect_not_before = 0.0
        self._warnings = RateLimitedLog(log)

    async def start(self) -> None:
        async with self._lock:
            if self._running:
                return
            self._running = True
            await self._ensure_publish_locked()
            await self._ensure_listen_locked()
            loop = asyncio.get_running_loop()
            self._tasks = [
                loop.create_task(self._supervise()),
                loop.create_task(self._flush_live_loop()),
                loop.create_task(self._sample_queue_usage()),
            ]

    async def close(self) -> None:
        async with self._lock:
            if not self._running:
                return
            self._running = False
            tasks = self._tasks
            self._tasks = []
            listen, self._listen = self._listen, None
            publish_conn, self._publish_conn = self._publish_conn, None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._flush_live_once()
        if listen is not None and not listen.is_closed():
            await listen.close()
        if publish_conn is not None and not publish_conn.is_closed():
            await publish_conn.close()

    async def publish(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        if not self._running and time.monotonic() < self._connect_not_before:
            self._dispatch(session_id, message)
            return
        if not self._running:
            try:
                await self.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("event bus start failed; local delivery only: %s", exc)
                self._note_remote_failure()
        self._dispatch(session_id, message)
        try:
            if message_seq(message) is not None:
                wake = wake_message(session_id, int(message_seq(message) or 0))
                wake["origin"] = self._origin
                await self._notify(wake)
            else:
                self._buffer_live(session_id, message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_notify_error(exc)
            self._note_remote_failure()

    def _buffer_live(self, session_id: uuid.UUID, message: dict[str, Any]) -> None:
        self._live.setdefault(session_id, []).append(
            {
                "type": message.get("type"),
                "session_id": str(session_id),
                "data": message.get("data", {}),
            }
        )

    def _note_notify_error(self, exc: BaseException) -> None:
        if self._metrics is not None:
            self._metrics.observe_event_bus_notify_error()
        self._warnings.warning(
            "event bus notify failed; the fallback poll covers it",
            event="event_bus.notify.failed",
            error_code="notify_failed",
            error=type(exc).__name__,
        )

    def _note_remote_failure(self) -> None:
        self._connect_not_before = time.monotonic() + _CONNECT_BACKOFF

    async def listen_instance(self, instance_id: str, handler: InstanceHandler) -> None:
        self._instances[instance_id] = handler
        conn = self._listen
        if conn is not None and not conn.is_closed():
            await conn.add_listener(
                instance_channel(instance_id), self._on_instance_notify
            )

    async def unlisten_instance(self, instance_id: str) -> None:
        self._instances.pop(instance_id, None)
        conn = self._listen
        if conn is not None and not conn.is_closed():
            with contextlib.suppress(Exception):
                await conn.remove_listener(
                    instance_channel(instance_id), self._on_instance_notify
                )

    async def send_instance(self, instance_id: str, message: dict[str, Any]) -> None:
        if not self._running:
            try:
                await self.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_notify_error(exc)
                self._note_remote_failure()
                return
        try:
            await self._notify(message, channel=instance_channel(instance_id))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._note_notify_error(exc)
            self._note_remote_failure()

    def _on_instance_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        channel: str,
        payload: str,
    ) -> None:
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            log.warning("event bus dropped malformed payload")
            return
        if not isinstance(message, dict):
            return
        for instance_id, handler in list(self._instances.items()):
            if instance_channel(instance_id) == channel:
                handler(message)

    async def _notify(
        self, payload: dict[str, Any], *, channel: str = EVENT_CHANNEL
    ) -> None:
        if time.monotonic() < self._connect_not_before:
            return
        raw = json.dumps(payload, separators=(",", ":"))
        if len(raw.encode("utf-8")) > NOTIFY_LIMIT:
            log.warning(
                "event bus payload over NOTIFY limit; dropping %s for %s",
                payload.get("kind"),
                payload.get("session_id"),
            )
            return
        async with self._notify_lock:
            conn = await self._ensure_publish()
            try:
                await asyncio.wait_for(
                    conn.execute("SELECT pg_notify($1, $2)", channel, raw),
                    timeout=NOTIFY_TIMEOUT,
                )
            except BaseException:
                self._drop_publish(conn)
                raise

    def _drop_publish(self, conn: asyncpg.Connection) -> None:
        if self._publish_conn is conn:
            self._publish_conn = None
        with contextlib.suppress(Exception):
            conn.terminate()

    async def _ensure_publish(self) -> asyncpg.Connection:
        async with self._lock:
            return await self._ensure_publish_locked()

    async def _ensure_publish_locked(self) -> asyncpg.Connection:
        conn = self._publish_conn
        if conn is None or conn.is_closed():
            conn = await asyncpg.connect(dsn=self._dsn, timeout=5)
            self._publish_conn = conn
        return conn

    async def _ensure_listen_locked(self) -> None:
        conn = self._listen
        if conn is not None and not conn.is_closed():
            return
        conn = await asyncpg.connect(dsn=self._dsn, timeout=5)
        await conn.add_listener(EVENT_CHANNEL, self._on_notify)
        for instance_id in self._instances:
            await conn.add_listener(
                instance_channel(instance_id), self._on_instance_notify
            )
        self._listen = conn

    def _on_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            log.warning("event bus dropped malformed payload")
            return
        if not isinstance(message, dict):
            return
        if message.get("origin") == self._origin:
            return
        raw_session = message.get("session_id")
        try:
            session_id = uuid.UUID(str(raw_session))
        except (ValueError, TypeError, AttributeError):
            return
        if message.get("kind") == "live":
            batch = message.get("batch")
            if isinstance(batch, list):
                for item in batch:
                    if isinstance(item, dict):
                        self._dispatch(session_id, item)
            return
        if is_wake(message) and isinstance(message.get("seq"), int):
            self._dispatch(session_id, message)

    async def _supervise(self) -> None:
        backoff_index = 0
        while self._running:
            await asyncio.sleep(1.0)
            if not self._running:
                return
            conn = self._listen
            if conn is not None and not conn.is_closed():
                backoff_index = 0
                continue
            try:
                async with self._lock:
                    if self._running:
                        await self._ensure_listen_locked()
            except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
                log.warning("event bus listen reconnect failed: %s", exc)
                await asyncio.sleep(_RECONNECT_BACKOFF[backoff_index])
                backoff_index = min(backoff_index + 1, len(_RECONNECT_BACKOFF) - 1)
                continue
            backoff_index = 0
            if self._metrics is not None:
                self._metrics.observe_event_bus_reconnect()
            log.warning("event bus listener reconnected")

    async def _flush_live_loop(self) -> None:
        await run_loop(
            "delta_flusher",
            self._flush_live_round,
            interval=LIVE_WINDOW,
            metrics=self._metrics,
        )

    async def _flush_live_round(self) -> None:
        if self._running:
            await self._flush_live_once()

    async def _flush_live_once(self) -> None:
        if not self._live:
            return
        pending = self._live
        self._live = {}
        for session_id, items in pending.items():
            if not items:
                continue
            try:
                for batch in split_notify_batches(
                    items, limit=NOTIFY_LIMIT - LIVE_HEADROOM
                ):
                    await self._notify(
                        {
                            "kind": "live",
                            "session_id": str(session_id),
                            "batch": batch,
                            "published_at": time.time(),
                            "origin": self._origin,
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._note_notify_error(exc)
                self._note_remote_failure()

    async def _sample_queue_usage(self) -> None:
        while self._running:
            await asyncio.sleep(_QUEUE_SAMPLE_INTERVAL)
            if not self._running or self._metrics is None:
                continue
            conn = self._listen
            if conn is None or conn.is_closed():
                continue
            try:
                value = await conn.fetchval("SELECT pg_notification_queue_usage()")
            except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
                log.warning("event bus queue usage sample failed: %s", exc)
                continue
            try:
                self._metrics.set_pg_notification_queue_usage(float(value))
            except (TypeError, ValueError):
                continue


def resolve_event_bus_name(settings: Settings, *, engine_url: str | None = None) -> str:
    """Return ``memory`` or ``postgres`` for the configured bus.

    ``auto`` (the default) is ``postgres`` on Postgres and ``memory``
    on SQLite. ``engine_url`` is the store engine URL when the store
    was injected rather than built from the settings; the bus must
    match the database it reads.
    """
    if settings.event_bus == "memory":
        return "memory"
    if settings.event_bus == "postgres":
        return "postgres"
    url = engine_url if engine_url is not None else settings.database_url
    return "postgres" if not is_sqlite_url(url) else "memory"


def create_event_bus(
    settings: Settings,
    *,
    store: Any | None = None,
    metrics: Any | None = None,
) -> EventBus:
    """Build the configured bus. Explicit ``postgres`` on SQLite fails."""
    engine_url = (
        store.engine.url.render_as_string(hide_password=False)
        if store is not None
        else None
    )
    name = resolve_event_bus_name(settings, engine_url=engine_url)
    if name == "postgres":
        checked = engine_url if engine_url is not None else settings.database_url
        if is_sqlite_url(checked):
            raise ConfigError("APIPI_EVENT_BUS=postgres needs a Postgres DATABASE_URL")
        dsn = postgres_url(checked).replace("postgresql+asyncpg://", "postgresql://", 1)
        return PostgresEventBus(dsn, metrics=metrics)
    return InMemoryEventBus()
