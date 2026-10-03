import logging
import uuid
from typing import Any

from starlette.websockets import WebSocket

from apipi.common.event_bus import EventBus
from apipi.common.logutil import log_event
from apipi.protocol import (
    SUPPORTED_FEATURES,
    HelloReply,
    RegisterMessage,
    RevokeEntry,
    StoreCheck,
    TtlEntry,
    peer_features,
)
from apipi.store.engine import Store
from apipi.store.models import WorkerToken
from apipi.store.repo import (
    bind_worker_token,
    get_worker_token,
    upsert_worker,
)
from apipi.workerhub.connection import WorkerConnection, WorkerImage, claimed_leases
from apipi.workerhub.hub import WorkerHub, heartbeat_interval

log = logging.getLogger("apipi.worker")


class TokenBindingError(Exception):
    """A register presented a worker id the token is not bound to."""

    def __init__(self, worker_id: uuid.UUID) -> None:
        super().__init__(str(worker_id))
        self.worker_id = worker_id


def accepts_for_register(register: RegisterMessage) -> frozenset[str]:
    if register.accepts is not None:
        return frozenset(register.accepts)
    if register.run_mode == "microvm":
        return frozenset({"none", "microvm"})
    return frozenset({"none"})


def _store_check_for(settings: Any) -> StoreCheck | None:
    """Issue a shared-root challenge for filesystem stores, else None."""
    if getattr(settings, "artifact_store", "local") != "local":
        return None
    from apipi.common.dirs import store_root
    from apipi.common.store_check import write_store_check

    root = store_root(settings)
    marker, nonce = write_store_check(root)
    return StoreCheck(marker=marker, nonce=nonce)


def verify_store_proof(
    settings: Any, marker: str, nonce: str, *, expected: tuple[str, str] | None
) -> bool:
    """Check a worker `store.proof` against the issued challenge."""
    if expected is None:
        return True
    if (marker, nonce) != expected:
        return False
    from apipi.common.dirs import store_root
    from apipi.common.store_check import read_store_check

    return read_store_check(store_root(settings), marker, nonce)


async def register_worker(
    hub: WorkerHub,
    store: Store,
    websocket: WebSocket,
    register: RegisterMessage,
    token: WorkerToken,
    event_hub: EventBus | None = None,
) -> WorkerConnection | None:
    run_mode = register.run_mode
    capacity = register.capacity
    memory_mb = register.memory_mb
    if memory_mb is None:
        memory_mb = capacity * hub.settings.microvm_mem_mib
    worker_id = register.id
    async with store.session() as db:
        current = await get_worker_token(db, token.id)
        if current is None or current.revoked_at is not None:
            return None
        if current.worker_id is None:
            if worker_id is None:
                worker_id = uuid.uuid4()
            await bind_worker_token(db, current, worker_id)
        elif worker_id is None:
            worker_id = current.worker_id
        elif current.worker_id != worker_id:
            raise TokenBindingError(current.worker_id)
    async with store.session() as db:
        row = await upsert_worker(
            db,
            worker_id,
            capacity=capacity,
            memory_mb=memory_mb,
            api_instance_id=hub.settings.instance_id,
        )
    conn = WorkerConnection(
        worker_id=row.id,
        generation=row.generation,
        websocket=websocket,
        capacity=row.capacity,
        memory_mb=row.memory_mb,
        run_mode=run_mode,
        token_id=token.id,
        images=images_for_register(register, run_mode),
        arch=register.arch,
        accepts=accepts_for_register(register),
        version=register.version or "unknown",
        features=peer_features(register.features),
    )
    conn.writer.bind(hub.metrics, hub.observe_send_queue)
    conn.writer.start()
    try:
        sessions = await hub.restore_leases(conn, store, register.running)
        store_check = _store_check_for(hub.settings)
        if store_check is not None:
            conn.store_proof = (store_check.marker, store_check.nonce)
        reported = claimed_leases(register.running)
        if event_hub is not None:
            revoke, ttl = await hub.reconcile_inventory(
                store,
                event_hub,
                conn.worker_id,
                reported,
                conn=conn,
                orphan_missing=bool(reported),
            )
        else:
            hub.note_inventory(conn.worker_id, reported)
            revoke, ttl = [], {}
        hello_sent = conn.writer.submit(
            HelloReply(
                worker_id=conn.worker_id,
                generation=conn.generation,
                connection_id=conn.connection_id,
                lease_ttl_seconds=hub.settings.worker_lease_ttl.total_seconds(),
                heartbeat_seconds=heartbeat_interval(hub.settings),
                features=sorted(SUPPORTED_FEATURES),
                sessions=sessions,
                store_check=store_check,
                revoke=[RevokeEntry.model_validate(entry) for entry in revoke],
                ttl={
                    uuid.UUID(key): TtlEntry.model_validate(value)
                    for key, value in ttl.items()
                },
            ).to_wire()
        )
        log_event(
            log,
            logging.INFO,
            "worker hello sent",
            event="worker.hello.sent",
            worker_id=conn.worker_id,
            connection_id=conn.connection_id,
            sessions=len(sessions),
            revoked=len(revoke),
        )
        await hub.resend_pending(conn)
        await hub.attach(conn)
        await hello_sent
    except BaseException:
        await hub.detach(conn.worker_id, conn)
        await conn.writer.stop()
        raise
    return conn


def legacy_images(arch: str | None = None) -> dict[str, WorkerImage]:
    images = {
        "default": WorkerImage("default", "legacy", "legacy", "S"),
    }
    if arch != "aarch64":
        images["browser"] = WorkerImage("browser", "legacy", "legacy", "M")
    return images


def images_for_register(
    register: RegisterMessage, run_mode: str
) -> dict[str, WorkerImage]:
    accepts = accepts_for_register(register)
    if register.images is None:
        return legacy_images(register.arch or None) if "microvm" in accepts else {}
    return {
        item.id: WorkerImage(item.id, item.version, item.digest, item.min_size)
        for item in register.images
    }
