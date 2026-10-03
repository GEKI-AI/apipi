import logging
import time
from typing import Any

from apipi.common.logutil import log_event
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


def _positive_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _run_mode(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def images_from_message(message: dict[str, Any], kind: str) -> dict[str, WorkerImage]:
    if "images" not in message:
        if kind == "microvm":
            raw_arch = message.get("arch")
            arch = raw_arch if isinstance(raw_arch, str) else None
            return legacy_images(arch)
        return {}
    raw = message.get("images")
    found: dict[str, WorkerImage] = {}
    if not isinstance(raw, list):
        return found
    for item in raw:
        if not isinstance(item, dict):
            continue
        image_id = item.get("id")
        if not isinstance(image_id, str) or not image_id:
            continue
        version = item.get("version")
        digest = item.get("digest")
        min_size = item.get("min_size")
        found[image_id] = WorkerImage(
            image_id,
            version if isinstance(version, str) else "",
            digest if isinstance(digest, str) else "",
            min_size if isinstance(min_size, str) else "S",
        )
    return found


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
    hub: WorkerHub, store: Store, conn: WorkerConnection, message: dict[str, Any]
) -> None:
    capacity = message.get("capacity")
    if capacity is not None and _positive_int(capacity) is None:
        return
    memory_mb = message.get("memory_mb")
    if memory_mb is not None and _positive_int(memory_mb) is None:
        return
    parsed_capacity = _positive_int(capacity) if capacity is not None else None
    parsed_memory = _positive_int(memory_mb) if memory_mb is not None else None
    parsed_mode = None
    if "run_mode" in message:
        parsed_mode = _run_mode(message.get("run_mode"))
        if parsed_mode is None:
            return
    observe_heartbeat(hub, conn)
    async with store.session() as db:
        await touch_worker(
            db,
            conn.worker_id,
            capacity=parsed_capacity,
            memory_mb=parsed_memory,
            api_instance_id=hub.settings.instance_id,
        )
        await extend_worker_leases(
            db,
            conn.worker_id,
            lease_until=utc_now() + hub.settings.worker_lease_ttl,
        )
    conn.last_renewed = time.monotonic()
    hub.observe_lease_event("renewed")
    if parsed_capacity is not None:
        conn.capacity = parsed_capacity
    if parsed_memory is not None:
        conn.memory_mb = parsed_memory
    if parsed_mode is not None:
        conn.run_mode = parsed_mode
        hub._observe()
    raw_accepts = message.get("accepts")
    if isinstance(raw_accepts, list) and raw_accepts:
        cleaned = {
            str(item).strip().lower()
            for item in raw_accepts
            if isinstance(item, str)
            and str(item).strip().lower() in {"none", "microvm"}
        }
        if cleaned:
            conn.accepts = frozenset(cleaned)
            hub._observe()
    raw_arch = message.get("arch")
    if isinstance(raw_arch, str) and raw_arch:
        conn.arch = raw_arch
    if "images" in message or parsed_mode is not None:
        kind = "microvm" if "microvm" in conn.accepts else "none"
        conn.images = images_from_message(message, kind)
    if message.get("drain") is True:
        conn.draining = True
        hub._observe()
    elif message.get("drain") is False:
        conn.draining = False
        hub._observe()
