from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apipi.common.event_bus import EventBus
from apipi.common.failures import (
    failure_for,
    log_extra,
    session_error_data,
)
from apipi.common.logutil import log_event
from apipi.services.session_events import persist_event
from apipi.services.turn_state import fail_stale_in_progress
from apipi.store.engine import Store
from apipi.store.repo import (
    agent_idle_ttls,
    clear_session_lease,
    get_sessions_by_ids,
    list_worker_leases,
)
from apipi.workerhub.connection import WorkerConnection

if TYPE_CHECKING:
    from apipi.workerhub.hub import WorkerHub

log = logging.getLogger("apipi.worker")


async def reconcile_inventory(
    hub: WorkerHub,
    store: Store,
    bus: EventBus,
    worker_id: uuid.UUID,
    reported: dict[uuid.UUID, uuid.UUID],
    unleased: Collection[uuid.UUID] = (),
    *,
    conn: WorkerConnection | None = None,
    orphan_missing: bool = True,
) -> tuple[list[dict[str, str]], dict[str, dict[str, Any]]]:
    """Compare the worker live set against the lease rows.

    With `orphan_missing` false, leases the worker did not report are left
    alone. Register does that for a worker that claims no sessions at
    all: it is a restart, and the spooled results it replays right after
    `hello` still need their lease. The first inventory then orphans
    what is really gone.

    Sessions leased to this worker but not reported are orphaned:
    the turn (if any) is failed and the lease is cleared, unless a
    command for that lease is still unacked (in flight to the
    worker, which cannot have reported it yet). Sessions the worker
    reports without a matching lease come back as `lease.revoke`
    entries. Every reported session that is still leased also gets
    its effective reaper TTL, so a restarted worker learns idle
    TTLs without reading the database itself. On-disk workspaces
    the worker reports as unleased get a TTL answer while their
    session row is still alive — a released lease only means the
    Pi stopped, while the workspace intentionally stays until its
    idle TTL — and a revoke (which tells the worker to wipe them)
    only once the row is gone. The TTL answer carries the idle
    baseline (`idle_since_epoch`, the row's last touch) so the
    reaper clock does not restart on every reply.
    """
    revoke: list[dict[str, str]] = []
    ttl: dict[str, dict[str, Any]] = {}
    ttl_rows: dict[str, Any] = {}
    if conn is None:
        conn = hub._conns.get(worker_id)
    hub.note_inventory(worker_id, reported)
    async with store.session() as db:
        rows = await list_worker_leases(db, worker_id)
        leased = {row.id: row for row in rows if row.lease_id is not None}
        for session_id, row in leased.items():
            claimed_lease = reported.get(session_id)
            if claimed_lease is not None and claimed_lease != row.lease_id:
                revoke.append(
                    {
                        "session_id": str(session_id),
                        "lease_id": str(claimed_lease),
                    }
                )
                continue
            if claimed_lease is None:
                if not orphan_missing or (
                    row.lease_id is not None and hub.commands.has_lease(row.lease_id)
                ):
                    # The command granting this lease is still in
                    # flight: the worker cannot have reported it yet,
                    # so this is not an orphan.
                    continue
                orphan = failure_for("worker_orphaned", "Worker lease orphaned")
                log_event(
                    log,
                    logging.ERROR,
                    "worker lease orphaned",
                    event="worker.lease.orphaned",
                    tenant_id=row.tenant_id,
                    session_id=row.id,
                    worker_id=worker_id,
                    lease_id=row.lease_id,
                    **log_extra(orphan),
                )
                hub.observe_lease_event("orphaned")
                await persist_event(
                    db,
                    bus,
                    row.tenant_id,
                    row.id,
                    type="agent.session.error",
                    data=session_error_data(orphan, mode="legacy"),
                )
                await clear_session_lease(db, row.tenant_id, row.id)
                await fail_stale_in_progress(db, bus, row.tenant_id, row.id)
                hub._forget_delta(session_id)
                if conn is not None and row.lease_id is not None:
                    conn.leases.discard(row.lease_id)
                    conn.lease_mem.pop(row.lease_id, None)
                continue
            ttl_rows[str(session_id)] = row
        for session_id, lease_id in reported.items():
            row = leased.get(session_id)
            if row is None:
                revoke.append(
                    {
                        "session_id": str(session_id),
                        "lease_id": str(lease_id),
                    }
                )
        pending = [
            session_id
            for session_id in dict.fromkeys(unleased)
            if session_id not in reported
            and str(session_id) not in ttl_rows
            and session_id not in leased
        ]
        found = await get_sessions_by_ids(db, pending)
        for session_id in dict.fromkeys(unleased):
            if session_id in reported or str(session_id) in ttl_rows:
                continue
            row = leased.get(session_id) or found.get(session_id)
            if row is None:
                # No session row anymore (a stopped session is
                # deleted): the directory is garbage, tell the
                # worker to wipe it.
                revoke.append({"session_id": str(session_id)})
            else:
                # The row is alive: a released lease only means
                # the Pi stopped, while the workspace stays until
                # its idle TTL. Answer the TTL so the normal
                # reaper wipes it when the TTL runs out.
                ttl_rows[str(session_id)] = row
        agent_ttl = await agent_idle_ttls(
            db, {row.agent_id for row in ttl_rows.values() if row.agent_id is not None}
        )
        for key, row in ttl_rows.items():
            ttl[key] = _inventory_ttl(
                hub, row, agent_ttl.get((row.tenant_id, row.agent_id))
            )
    for entry in revoke:
        if "lease_id" in entry:
            hub.observe_lease_event("revoked")
            log_event(
                log,
                logging.INFO,
                "worker lease revoked",
                event="worker.lease.revoked",
                worker_id=worker_id,
                session_id=entry["session_id"],
                lease_id=entry["lease_id"],
                reason="not_leased",
            )
    hub._observe()
    return revoke, ttl


def _inventory_ttl(hub: WorkerHub, row: Any, agent_idle: str | None) -> dict[str, Any]:
    """The effective reaper TTL answer for one session row.

    `idle_since_epoch` is the row's last touch, so the worker
    reaper measures true idleness: re-answering the TTL on every
    inventory must not restart the clock, or an idle workspace
    would never be reaped.
    """
    from apipi.common.idle import resolve_idle_ttl

    environment = row.environment if isinstance(row.environment, dict) else {}
    env_type = environment.get("type")
    meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
    resolved = resolve_idle_ttl(
        hub.settings,
        env_type if isinstance(env_type, str) else None,
        session_idle=row.idle_ttl,
        session_metadata=meta,
        agent_idle=agent_idle,
    )
    return {
        "idle_ttl_seconds": resolved.total_seconds() if resolved is not None else None,
        "env_type": env_type,
        "idle_since_epoch": _idle_since_epoch(row.updated_at),
    }


def _idle_since_epoch(updated_at: Any) -> float | None:
    """Unix baseline for the reaper idle clock (stable across replies)."""
    if not isinstance(updated_at, datetime):
        return None
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return updated_at.timestamp()
