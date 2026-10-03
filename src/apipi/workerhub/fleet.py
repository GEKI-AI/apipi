"""Placement over the whole fleet: workers on this replica and on others.

A worker on this replica is read from its live connection, which is
authoritative. A worker on another replica is read from its `workers`
row (written on register and on every heartbeat) and from the leased
session rows. The row can be one heartbeat old, so the replica that
holds the socket checks capacity again when it grants the lease.
"""

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apipi.common.sandbox import mem_mib_for_size, sandbox_size_of
from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.models import WorkerRow, utc_now
from apipi.store.repo import list_leased_environments, list_live_workers
from apipi.workerhub.connection import WorkerConnection


@dataclass
class Candidate:
    worker_id: uuid.UUID
    accepts: frozenset[str]
    images: frozenset[str]
    arch: str
    draining: bool
    capacity: int
    memory_mb: int
    leases: int
    used_mem: int
    instance: str | None = None
    conn: WorkerConnection | None = None


def local_candidates(
    conns: Mapping[uuid.UUID, WorkerConnection], session_mem: int
) -> list[Candidate]:
    return [
        Candidate(
            worker_id=conn.worker_id,
            accepts=conn.accepts,
            images=frozenset(conn.images),
            arch=conn.arch,
            draining=conn.draining,
            capacity=conn.capacity,
            memory_mb=conn.memory_mb,
            leases=len(conn.leases),
            used_mem=sum(
                conn.lease_mem.get(lease, session_mem) for lease in conn.leases
            ),
            conn=conn,
        )
        for conn in conns.values()
    ]


def is_fresh(last_seen: datetime | None, settings: Settings) -> bool:
    """True when the worker was heard from within the lease TTL."""
    if last_seen is None:
        return False
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    return utc_now() - last_seen <= timedelta(
        seconds=settings.worker_lease_ttl.total_seconds()
    )


def row_candidate(
    row: WorkerRow, environments: Iterable[dict[str, object]], settings: Settings
) -> Candidate:
    envs = list(environments)
    return Candidate(
        worker_id=row.id,
        accepts=frozenset(row.accepts or ["none"]),
        images=frozenset(str(item) for item in (row.images or [])),
        arch=row.arch or "",
        draining=bool(row.draining),
        capacity=row.capacity,
        memory_mb=row.memory_mb,
        leases=len(envs),
        used_mem=sum(
            mem_mib_for_size(settings, sandbox_size_of(env))  # type: ignore[arg-type]
            for env in envs
        ),
        instance=row.api_instance_id,
    )


async def remote_candidates(
    store: Store, settings: Settings, instance_id: str
) -> list[Candidate]:
    """Workers whose socket is on another replica, as of their last heartbeat."""
    seen_after = utc_now() - timedelta(
        seconds=settings.worker_lease_ttl.total_seconds()
    )
    async with store.session() as db:
        rows = await list_live_workers(
            db, seen_after=seen_after, exclude_instance=instance_id
        )
        leased = await list_leased_environments(db, [row.id for row in rows])
    by_worker: dict[uuid.UUID, list[dict[str, object]]] = {}
    for worker_id, environment in leased:
        by_worker.setdefault(worker_id, []).append(environment)
    return [row_candidate(row, by_worker.get(row.id, []), settings) for row in rows]


def choose(
    candidates: Iterable[Candidate],
    *,
    kind: str,
    session_mem: int,
    image: str | None = None,
) -> Candidate | None:
    """The least loaded worker that accepts the kind, has the image, and has room."""
    ready: list[Candidate] = []
    for item in candidates:
        if kind not in item.accepts:
            continue
        if image is not None and kind == "microvm" and image not in item.images:
            continue
        if item.draining:
            continue
        if item.leases + 1 > item.capacity:
            continue
        if item.used_mem + session_mem > item.memory_mb:
            continue
        ready.append(item)
    if not ready:
        return None
    ready.sort(key=lambda item: (-(item.memory_mb - item.used_mem), item.leases))
    return ready[0]


def has_image(candidates: Iterable[Candidate], kind: str, image: str | None) -> bool:
    if image is None or kind != "microvm":
        return True
    return any(kind in item.accepts and image in item.images for item in candidates)


def image_arches(candidates: Iterable[Candidate], image: str) -> set[str]:
    return {
        item.arch
        for item in candidates
        if "microvm" in item.accepts and item.arch and image not in item.images
    }
