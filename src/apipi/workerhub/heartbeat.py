import logging
import time
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from apipi.protocol import HeartbeatMessage, WorkerImageInfo
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    extend_worker_leases,
    touch_worker,
)
from apipi.workerhub.connection import WorkerConnection, WorkerImage
from apipi.workerhub.hub import WorkerHub
from apipi.workerhub.register import legacy_images

log = logging.getLogger("apipi.worker")


def _run_mode(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def worker_images(items: Iterable[WorkerImageInfo]) -> dict[str, WorkerImage]:
    return {
        item.id: WorkerImage(item.id, item.version, item.digest, item.min_size)
        for item in items
    }


def images_from_heartbeat(
    heartbeat: HeartbeatMessage, kind: str
) -> dict[str, WorkerImage]:
    """The images a heartbeat reports.

    A heartbeat without an `images` field comes from a worker that does
    not list its images: a microvm worker then gets the legacy set.
    """
    if "images" not in heartbeat.model_fields_set:
        return legacy_images(heartbeat.arch) if kind == "microvm" else {}
    return worker_images(heartbeat.images or [])


def images_from_message(message: dict[str, Any], kind: str) -> dict[str, WorkerImage]:
    arch = message.get("arch")
    try:
        heartbeat = HeartbeatMessage.model_validate(
            {
                "images": message["images"],
                "arch": arch if isinstance(arch, str) else None,
            }
            if "images" in message
            else {"arch": arch if isinstance(arch, str) else None}
        )
    except ValidationError:
        return {}
    return images_from_heartbeat(heartbeat, kind)


def observe_heartbeat(hub: WorkerHub, conn: WorkerConnection) -> None:
    """Record the gap since the previous heartbeat and warn when it is late."""
    now = time.monotonic()
    previous = (
        conn.last_heartbeat if conn.last_heartbeat is not None else (conn.connected_at)
    )
    gap = now - previous
    conn.last_heartbeat = now
    if hub.metrics is not None:
        hub.metrics.observe_worker_heartbeat_gap(gap)
    ttl = hub.settings.worker_lease_ttl.total_seconds()
    if gap > ttl / 2:
        conn.warnings.warning(
            "worker heartbeat late",
            event="worker.heartbeat.late",
            error_code="heartbeat_late",
            worker_id=conn.worker_id,
            source="api",
            gap_seconds=round(gap, 3),
            lease_ttl_seconds=ttl,
        )


def parse_heartbeat(
    hub: WorkerHub, conn: WorkerConnection, message: dict[str, Any]
) -> HeartbeatMessage:
    """Parse a heartbeat; an invalid optional field is dropped, not fatal.

    A heartbeat renews the worker's leases, so one bad `capacity`,
    `memory_mb`, `run_mode`, or `images` entry must not cost the lease.
    Each invalid field is logged and ignored.
    """
    try:
        return HeartbeatMessage.model_validate(message)
    except ValidationError as exc:
        invalid = {
            str(error["loc"][0]) for error in exc.errors() if error.get("loc")
        } - {"type"}
        hub.observe_protocol("heartbeat.field_ignored")
        conn.warnings.warning(
            "worker heartbeat field ignored",
            event="worker.heartbeat.field_ignored",
            error_code="heartbeat_field_invalid",
            worker_id=conn.worker_id,
            fields=sorted(invalid),
        )
        cleaned = {key: value for key, value in message.items() if key not in invalid}
        return HeartbeatMessage.model_validate(cleaned)


def _fleet_state(
    conn: WorkerConnection, heartbeat: HeartbeatMessage, parsed_mode: str | None
) -> tuple[frozenset[str], dict[str, WorkerImage], str, bool]:
    """The accepts, images, arch, and drain state after this heartbeat."""
    accepts = conn.accepts
    if heartbeat.accepts:
        cleaned = {
            item.strip().lower()
            for item in heartbeat.accepts
            if item.strip().lower() in {"none", "microvm"}
        }
        if cleaned:
            accepts = frozenset(cleaned)
    arch = heartbeat.arch or conn.arch
    images = conn.images
    if "images" in heartbeat.model_fields_set or parsed_mode is not None:
        kind = "microvm" if "microvm" in accepts else "none"
        images = images_from_heartbeat(heartbeat, kind)
    draining = conn.draining if heartbeat.drain is None else heartbeat.drain is True
    return accepts, images, arch, draining


async def heartbeat_worker(
    hub: WorkerHub, store: Store, conn: WorkerConnection, heartbeat: HeartbeatMessage
) -> bool:
    """Record one heartbeat and extend the leases; False for a superseded connection."""
    fields = heartbeat.model_fields_set
    parsed_mode = None
    if "run_mode" in fields:
        parsed_mode = _run_mode(heartbeat.run_mode)
        if parsed_mode is None:
            hub.observe_protocol("heartbeat.field_ignored")
            conn.warnings.warning(
                "worker heartbeat field ignored",
                event="worker.heartbeat.field_ignored",
                error_code="heartbeat_field_invalid",
                worker_id=conn.worker_id,
                fields=["run_mode"],
            )
    observe_heartbeat(hub, conn)
    state = _fleet_state(conn, heartbeat, parsed_mode)
    async with store.session() as db:
        touched = await touch_worker(
            db,
            conn.worker_id,
            capacity=heartbeat.capacity,
            memory_mb=heartbeat.memory_mb,
            api_instance_id=hub.instance_id,
            generation=conn.generation,
            fleet={
                "accepts": sorted(state[0]),
                "images": sorted(state[1]),
                "arch": state[2] or None,
                "draining": state[3],
            },
        )
        if touched is not None and touched.generation != conn.generation:
            return False
        await extend_worker_leases(
            db,
            conn.worker_id,
            lease_until=utc_now() + hub.settings.worker_lease_ttl,
            generation=conn.generation if touched is not None else None,
        )
    conn.last_renewed = time.monotonic()
    hub.observe_lease_event("renewed")
    if heartbeat.capacity is not None:
        conn.capacity = heartbeat.capacity
    if heartbeat.memory_mb is not None:
        conn.memory_mb = heartbeat.memory_mb
    if parsed_mode is not None:
        conn.run_mode = parsed_mode
        hub._observe()
    accepts, images, arch, draining = state
    if accepts != conn.accepts:
        hub._observe()
    conn.accepts = accepts
    conn.arch = arch
    conn.images = images
    if draining != conn.draining:
        hub._observe()
    conn.draining = draining
    return True
