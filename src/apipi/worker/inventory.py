import logging
import time
import uuid
from typing import Any

from pydantic import ValidationError

from apipi.protocol import RevokeEntry, TtlEntry, WorkspaceReapedPayload
from apipi.worker.commands import CommandDedupe

log = logging.getLogger("apipi.worker")


def _seed_reaper_ttl(execution: Any, ttl: Any) -> None:
    """Teach the reaper idle TTLs from an inventory reply (no DB read)."""
    remember = getattr(execution, "_context_ttl", None)
    if not isinstance(remember, dict) or not isinstance(ttl, dict):
        return
    now = time.time()
    for raw_id, entry in ttl.items():
        try:
            session_id = str(uuid.UUID(str(raw_id)))
        except (ValueError, TypeError):
            continue
        try:
            parsed = TtlEntry.model_validate(entry)
        except ValidationError:
            continue
        raw_seconds = parsed.idle_ttl_seconds
        seconds = (
            float(raw_seconds) if raw_seconds is not None and raw_seconds >= 0 else None
        )
        raw_since = parsed.idle_since_epoch
        if raw_since is not None and 0 < raw_since <= now:
            last_seen = float(raw_since)
        else:
            last_seen = now
        known = remember.get(session_id)
        if known is not None:
            # Never move the reaper clock backwards: the worker's own
            # turn activity (`refresh_context_seen`) is newer than the
            # row touch whenever the row is not updated per turn.
            last_seen = max(last_seen, known[1])
        remember[session_id] = (seconds, last_seen, parsed.env_type)


async def _apply_inventory_reply(
    execution: Any,
    reply: dict[str, Any],
    *,
    session_leases: dict[uuid.UUID, str] | None = None,
    outbox: Any | None = None,
    relay: Any | None = None,
    dedupe: "CommandDedupe | None" = None,
    settings: Any | None = None,
) -> None:
    """Apply revocations and reaper TTLs from an inventory reply."""
    _seed_reaper_ttl(execution, reply.get("ttl"))
    revoke = reply.get("revoke")
    if not isinstance(revoke, list):
        return
    for entry in revoke:
        try:
            session_id = RevokeEntry.model_validate(entry).session_id
        except ValidationError:
            continue
        if dedupe is not None:
            dedupe.forget(session_id)
        known = session_leases is not None and session_id in session_leases
        if session_leases is not None:
            session_leases.pop(session_id, None)
        if relay is not None:
            forget = getattr(relay, "forget", None)
            if callable(forget):
                forget(session_id)
        await execution.teardown(session_id)
        if outbox is not None:
            drop = getattr(outbox, "drop_session", None)
            if callable(drop):
                drop(session_id)
        if settings is not None and not known:
            # Revoked without a local lease: an on-disk workspace the
            # worker only reported as unleased. Wipe it so reaped
            # leftovers do not accumulate after a restart.
            await wipe_unknown_workspace(settings, execution, outbox, session_id)
        log.info("worker revoked session", extra={"session_id": str(session_id)})


async def wipe_unknown_workspace(
    settings: Any, execution: Any, outbox: Any | None, session_id: uuid.UUID
) -> None:
    """Wipe an on-disk workspace the worker holds no lease for.

    Runs after teardown for revokes of sessions absent from the local
    lease set (typically unleased dirs reported in the inventory). The
    pool liveness guard keeps a racing fresh turn safe, and the receipt
    lets the API delete the session blobs.
    """
    pool = getattr(execution, "pool", None)
    try:
        if pool is not None and (pool.alive(session_id) or pool.held(session_id)):
            return
    except Exception:
        return
    from apipi.common.dirs import sessions_root, wipe_workspace

    try:
        root = sessions_root(settings)
    except Exception:
        return
    try:
        tenants = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return
    wiped = False
    for tenant_dir in tenants:
        if tenant_dir.name.startswith("."):
            continue
        try:
            uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        workspace = tenant_dir / str(session_id)
        try:
            if not workspace.is_dir():
                continue
        except OSError:
            continue
        try:
            wipe_workspace(workspace)
        except Exception:
            log.exception(
                "unknown workspace wipe failed",
                extra={"session_id": str(session_id)},
            )
            continue
        wiped = True
    if wiped and outbox is not None:
        append = getattr(outbox, "append", None)
        if callable(append):
            try:
                append(
                    session_id,
                    "workspace.reaped",
                    WorkspaceReapedPayload(reason="revoked"),
                )
            except Exception:
                log.exception(
                    "revoked workspace report failed",
                    extra={"session_id": str(session_id)},
                )


def _unleased_session_dirs(
    settings: Any,
    session_leases: dict[uuid.UUID, str],
    pool: Any | None,
) -> list[uuid.UUID]:
    """On-disk session workspaces the worker holds no lease for.

    These ride along in the periodic inventory so the API answers
    with a reaper TTL while the session row is alive and a revoke
    (which wipes them) only once the row is gone. Live or held
    guests are never reported.
    """
    from apipi.common.dirs import sessions_root

    try:
        root = sessions_root(settings)
    except Exception:
        return []
    try:
        tenants = [entry for entry in root.iterdir() if entry.is_dir()]
    except OSError:
        return []
    found: list[uuid.UUID] = []
    for tenant_dir in tenants:
        if tenant_dir.name.startswith("."):
            continue
        try:
            uuid.UUID(tenant_dir.name)
        except ValueError:
            continue
        try:
            sessions = [entry for entry in tenant_dir.iterdir() if entry.is_dir()]
        except OSError:
            continue
        for session_dir in sessions:
            try:
                session_id = uuid.UUID(session_dir.name)
            except ValueError:
                continue
            if session_id in session_leases or session_id in found:
                continue
            if pool is not None:
                try:
                    if pool.alive(session_id) or pool.held(session_id):
                        continue
                except Exception:
                    continue
            found.append(session_id)
    return found
