"""Worker-side lifecycle reporting through the outbox."""

import uuid
from datetime import datetime
from typing import Any, Protocol

from apipi.common.timefmt import utc_ts


class LifecycleReporter(Protocol):
    """The pool slot for session lifecycle events."""

    active: bool

    def emit_start(self, fields: dict[str, Any], *, cause: str) -> int | None: ...

    def emit_stop(
        self, fields: dict[str, Any], *, reason: str, live_ms: int
    ) -> int | None: ...


LIFECYCLE_WORKER_ENV_PREFIX = "APIPI_LIFECYCLE_"


def worker_lifecycle_ignored() -> list[str]:
    """Lifecycle settings set on a worker; the API owns export now."""
    import os

    return sorted(
        name for name in os.environ if name.startswith(LIFECYCLE_WORKER_ENV_PREFIX)
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return utc_ts(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class OutboxLifecycleReporter:
    """Report pool lifecycle over the worker socket instead of exporting.

    The split worker holds no export URL or token: session live
    start/stop go out as durable v2 envelopes through the outbox, so
    they survive disconnects and replay after reconnect. The API
    persists and exports them. Heartbeats need no envelope; the
    periodic inventory live set is what the API derives them from.

    It implements the `LifecycleReporter` slot of the pool.
    """

    heartbeat_s: float | None = None

    def __init__(self, outbox: Any, worker_id: str | None = None) -> None:
        self.outbox = outbox
        self.worker_id = worker_id
        self.active = True
        self.metrics = None
        self._queue = None

    def set_worker_id(self, worker_id: str | None) -> None:
        self.worker_id = worker_id or None

    def emit_start(self, fields: dict[str, Any], *, cause: str) -> int | None:
        raw_session = fields.get("session_id")
        try:
            session_id = (
                raw_session
                if isinstance(raw_session, uuid.UUID)
                else uuid.UUID(str(raw_session))
            )
        except (ValueError, TypeError):
            return None
        # Only the envelope payload keys travel: identity comes from the
        # session row on the API, and anything else would fail the
        # strict payload validation there.
        payload = {"cause": cause}
        for key in (
            "environment_type",
            "sandbox_size",
            "sandbox_image",
            "image_version",
            "image_digest",
            "run_mode",
            "started_at",
        ):
            payload[key] = _jsonable(fields.get(key))
        try:
            envelope = self.outbox.append(session_id, "lifecycle.start", payload)
        except Exception:
            return None
        return int(envelope.get("seq") or 0) or None

    def emit_stop(
        self, fields: dict[str, Any], *, reason: str, live_ms: int
    ) -> int | None:
        raw_session = fields.get("session_id")
        try:
            session_id = (
                raw_session
                if isinstance(raw_session, uuid.UUID)
                else uuid.UUID(str(raw_session))
            )
        except (ValueError, TypeError):
            return None
        payload: dict[str, Any] = {
            "reason": reason,
            "live_ms": max(live_ms, 0),
            "started_at": _jsonable(fields.get("started_at")),
        }
        start_seq = fields.get("start_seq")
        if isinstance(start_seq, int) and start_seq >= 1:
            payload["start_seq"] = start_seq
        try:
            envelope = self.outbox.append(session_id, "lifecycle.stop", payload)
        except Exception:
            return None
        return int(envelope.get("seq") or 0) or None

    def emit_heartbeat(self, entries: list[dict[str, Any]]) -> int | None:
        del entries
        return None

    def start(self) -> None:
        return None

    async def flush(self, timeout: float | None = None) -> None:
        del timeout
        return None

    async def close(self) -> None:
        return None
