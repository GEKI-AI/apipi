import logging
import time
from collections.abc import Iterable
from typing import Any

from pydantic import ValidationError

from apipi.common.logutil import log_event
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
        log_event(
            log,
            logging.WARNING,
            "worker heartbeat late",
            event="worker.heartbeat.late",
            worker_id=conn.worker_id,
            source="api",
            gap_seconds=round(gap, 3),
            lease_ttl_seconds=ttl,
        )


async def heartbeat_worker(
    hub: WorkerHub, store: Store, conn: WorkerConnection, heartbeat: HeartbeatMessage
) -> None:
    fields = heartbeat.model_fields_set
    parsed_mode = None
    if "run_mode" in fields:
        parsed_mode = _run_mode(heartbeat.run_mode)
        if parsed_mode is None:
            return
    observe_heartbeat(hub, conn)
    async with store.session() as db:
        await touch_worker(
            db,
            conn.worker_id,
            capacity=heartbeat.capacity,
            memory_mb=heartbeat.memory_mb,
            api_instance_id=hub.settings.instance_id,
        )
        await extend_worker_leases(
            db,
            conn.worker_id,
            lease_until=utc_now() + hub.settings.worker_lease_ttl,
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
    if heartbeat.accepts:
        cleaned = {
            item.strip().lower()
            for item in heartbeat.accepts
            if item.strip().lower() in {"none", "microvm"}
        }
        if cleaned:
            conn.accepts = frozenset(cleaned)
            hub._observe()
    if heartbeat.arch:
        conn.arch = heartbeat.arch
    if "images" in fields or parsed_mode is not None:
        kind = "microvm" if "microvm" in conn.accepts else "none"
        conn.images = images_from_heartbeat(heartbeat, kind)
    if heartbeat.drain is True:
        conn.draining = True
        hub._observe()
    elif heartbeat.drain is False:
        conn.draining = False
        hub._observe()
