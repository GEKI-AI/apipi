import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update

from apipi.config import Settings
from apipi.services.event_bus import EventBus
from apipi.services.runtime import persist_event
from apipi.store.engine import Store
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import get_session, get_session_environment, update_environment

SEEN_INTERVAL = timedelta(seconds=5)
STALE_AFTER = SEEN_INTERVAL * 3
EAGER_KEY = "apipi.sandbox_eager_boot"
HOSTED = frozenset({"openai_hosted"})

_OPENAI_STATUS = {
    "none": "disconnected",
    "starting": "provisioning",
    "ready": "connected",
    "stopped": "disconnected",
    "failed": "failed",
}
_ROW_STATUS = {
    "starting": "pending",
    "ready": "connected",
    "stopped": "disconnected",
    "failed": "failed",
}
_STORED_STATUS = {
    "pending": "provisioning",
    "connected": "connected",
    "disconnected": "disconnected",
    "failed": "failed",
}


def is_hosted(environment: dict[str, Any] | None) -> bool:
    if not isinstance(environment, dict):
        return False
    return environment.get("type") in HOSTED


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return False


def eager_boot_enabled(
    settings: Settings,
    *,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    session_defaults: dict[str, Any] | None = None,
) -> bool:
    if isinstance(session_metadata, dict) and EAGER_KEY in session_metadata:
        return _as_bool(session_metadata.get(EAGER_KEY))
    if isinstance(agent_metadata, dict) and EAGER_KEY in agent_metadata:
        return _as_bool(agent_metadata.get(EAGER_KEY))
    return settings.sandbox_eager_boot


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def effective_state(
    row: SessionRow, *, now: datetime | None = None
) -> tuple[str, str | None]:
    state = row.sandbox_state or "none"
    reason = row.sandbox_reason
    if state not in {"starting", "ready"}:
        return state, reason
    seen = row.sandbox_seen_at or row.sandbox_since
    if seen is None:
        return state, reason
    current = now or utc_now()
    if _aware(current) - _aware(seen) >= STALE_AFTER:
        return "stopped", "worker_lost"
    return state, reason


def sandbox_public(
    row: SessionRow, *, now: datetime | None = None
) -> dict[str, Any] | None:
    environment = row.environment if isinstance(row.environment, dict) else {}
    if not is_hosted(environment):
        return None
    state, reason = effective_state(row, now=now)
    image = row.sandbox_image
    if not isinstance(image, str) or not image:
        raw = environment.get("sandbox_image")
        image = raw if isinstance(raw, str) else None
    size = row.sandbox_size
    if not isinstance(size, str) or not size:
        raw_size = environment.get("sandbox_size")
        size = raw_size if isinstance(raw_size, str) else None
    since = row.sandbox_since
    return {
        "state": state,
        "reason": reason,
        "since": since.isoformat() if since is not None else None,
        "image": image,
        "image_version": row.sandbox_image_version,
        "size": size,
        "cold_boots": row.sandbox_cold_boots or 0,
        "last_boot_ms": row.sandbox_last_boot_ms,
    }


def environment_status(row: SessionRow, *, now: datetime | None = None) -> str | None:
    environment = row.environment if isinstance(row.environment, dict) else {}
    if not is_hosted(environment):
        return None
    state, _reason = effective_state(row, now=now)
    return _OPENAI_STATUS.get(state, "disconnected")


_CONTAINER = {"S": "small", "M": "medium", "L": "large"}


def overlay_environment(row: SessionRow) -> dict[str, Any]:
    environment = dict(row.environment) if isinstance(row.environment, dict) else {}
    environment.pop("directory", None)
    size = environment.get("sandbox_size")
    if isinstance(size, str) and size in _CONTAINER:
        environment["container_size"] = _CONTAINER[size]
    if is_hosted(environment):
        environment["status"] = environment_status(row)
        environment["sandbox"] = sandbox_public(row)
        return environment
    environment["sandbox"] = None
    return environment


def _env_id(environment: dict[str, Any]) -> str | None:
    raw = environment.get("id")
    return raw if isinstance(raw, str) and raw else None


def _sandbox_data(
    environment: dict[str, Any], sandbox: dict[str, Any]
) -> dict[str, Any]:
    data: dict[str, Any] = {"sandbox": sandbox}
    env_id = _env_id(environment)
    if env_id is not None:
        data["environment_id"] = env_id
    return data


async def _sync_environment_row(
    db: Any, tenant_id: uuid.UUID, session_id: uuid.UUID, state: str
) -> None:
    status = _ROW_STATUS.get(state)
    if status is None:
        return
    env = await get_session_environment(db, tenant_id, session_id)
    if env is None:
        return
    await update_environment(db, tenant_id, env.id, status=status)


async def apply_transition(
    db: Any,
    row: SessionRow,
    phase: str,
    fields: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Apply one sandbox phase to a hosted session row.

    Updates the `sandbox_*` columns and builds the public
    `environment.*` event body, shared by the local transition path
    and the split-mode `sandbox.status` ingest so the two cannot drift.
    Returns the event type and body, or None for an unknown phase.
    The caller persists the event itself.
    """
    environment = row.environment if isinstance(row.environment, dict) else {}
    now = utc_now()
    row.sandbox_seen_at = now
    row.sandbox_since = now
    worker_id = fields.get("worker_id")
    if isinstance(worker_id, uuid.UUID):
        row.sandbox_worker_id = worker_id
    elif isinstance(worker_id, str) and worker_id:
        with contextlib.suppress(ValueError):
            row.sandbox_worker_id = uuid.UUID(worker_id)
    if phase == "starting":
        row.sandbox_state = "starting"
        row.sandbox_reason = None
        image = fields.get("image")
        size = fields.get("size")
        if isinstance(image, str):
            row.sandbox_image = image
        if isinstance(size, str):
            row.sandbox_size = size
        event_type = "agent.session.environment.pending"
        data = _sandbox_data(
            environment,
            {
                "state": "starting",
                "cold": True,
                "cause": fields.get("cause") or "spawn",
                "image": row.sandbox_image,
                "size": row.sandbox_size,
            },
        )
    elif phase == "ready":
        row.sandbox_state = "ready"
        row.sandbox_reason = None
        row.sandbox_cold_boots = (row.sandbox_cold_boots or 0) + 1
        image = fields.get("image")
        version = fields.get("image_version")
        size = fields.get("size")
        boot_ms = fields.get("boot_ms")
        if isinstance(image, str):
            row.sandbox_image = image
        if isinstance(version, str):
            row.sandbox_image_version = version
        if isinstance(size, str):
            row.sandbox_size = size
        if isinstance(boot_ms, int):
            row.sandbox_last_boot_ms = boot_ms
        event_type = "agent.session.environment.connected"
        data = _sandbox_data(
            environment,
            {
                "state": "ready",
                "image": row.sandbox_image,
                "image_version": row.sandbox_image_version,
                "size": row.sandbox_size,
                "run_mode": fields.get("run_mode"),
                "boot_ms": boot_ms if isinstance(boot_ms, int) else 0,
                "lock_wait_ms": fields.get("lock_wait_ms") or 0,
                "setup_ms": fields.get("setup_ms") or 0,
            },
        )
    elif phase == "stopped":
        row.sandbox_state = "stopped"
        reason = fields.get("reason")
        row.sandbox_reason = reason if isinstance(reason, str) else "stop"
        event_type = "agent.session.environment.disconnected"
        data = _sandbox_data(
            environment,
            {
                "state": "stopped",
                "reason": row.sandbox_reason,
                "live_ms": fields.get("live_ms") or 0,
            },
        )
    else:
        return None
    state = row.sandbox_state
    if state is not None:
        await _sync_environment_row(db, row.tenant_id, row.id, state)
    return event_type, data


async def record_transition(
    store: Store,
    hub: EventBus,
    session_id: uuid.UUID,
    phase: str,
    fields: dict[str, Any],
) -> None:
    tenant_id = fields.get("tenant_id")
    if not isinstance(tenant_id, uuid.UUID):
        return
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is None or not is_hosted(row.environment):
            return
        applied = await apply_transition(db, row, phase, fields)
        if applied is None:
            return
        event_type, data = applied
        await persist_event(db, hub, tenant_id, session_id, type=event_type, data=data)


async def note_failed(
    db: Any,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"error": message}
    if code:
        data["code"] = code
    if db is None:
        # A split worker holds no database: the API applies the
        # sandbox failure when it ingests the reported envelopes.
        return data
    row = await get_session(db, tenant_id, session_id)
    if row is None or not is_hosted(row.environment):
        return data
    environment = row.environment if isinstance(row.environment, dict) else {}
    now = utc_now()
    row.sandbox_state = "failed"
    row.sandbox_reason = code
    row.sandbox_since = now
    row.sandbox_seen_at = now
    sandbox: dict[str, Any] = {"state": "failed"}
    if code:
        sandbox["code"] = code
    data.update(_sandbox_data(environment, sandbox))
    await _sync_environment_row(db, tenant_id, session_id, "failed")
    return data


async def expire_if_stale(
    db: Any, hub: EventBus, tenant_id: uuid.UUID, row: SessionRow
) -> SessionRow:
    if not is_hosted(row.environment):
        return row
    state, reason = effective_state(row)
    if state == row.sandbox_state or reason != "worker_lost":
        return row
    environment = row.environment if isinstance(row.environment, dict) else {}
    now = utc_now()
    row.sandbox_state = "stopped"
    row.sandbox_reason = "worker_lost"
    row.sandbox_since = now
    await _sync_environment_row(db, tenant_id, row.id, "stopped")
    await persist_event(
        db,
        hub,
        tenant_id,
        row.id,
        type="agent.session.environment.disconnected",
        data=_sandbox_data(
            environment,
            {"state": "stopped", "reason": "worker_lost", "live_ms": 0},
        ),
    )
    return row


async def touch_seen(store: Store, session_ids: list[uuid.UUID]) -> None:
    if not session_ids:
        return
    async with store.session() as db:
        await db.execute(
            update(SessionRow)
            .where(
                SessionRow.id.in_(session_ids),
                SessionRow.sandbox_state.in_(("starting", "ready")),
            )
            .values(sandbox_seen_at=utc_now())
        )


def environment_public(row: SessionRow, stored: str | None) -> dict[str, Any]:
    environment = row.environment if isinstance(row.environment, dict) else {}
    env_type = environment.get("type")
    if is_hosted(environment):
        status = environment_status(row) or "disconnected"
        sandbox = sandbox_public(row)
    else:
        status = stored_status(stored)
        sandbox = None
    raw_id = environment.get("id")
    return {
        "id": raw_id if isinstance(raw_id, str) else None,
        "type": env_type,
        "status": status,
        "sandbox": sandbox,
    }


def stored_status(status: str | None) -> str:
    if status is None:
        return "disconnected"
    return _STORED_STATUS.get(status, "disconnected")
