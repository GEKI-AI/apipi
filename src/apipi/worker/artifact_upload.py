"""Worker-side artifact uploads without object-store credentials (#448).

S3: the worker asks the API for a presigned PUT via the durable
`artifact.presign` outbox envelope, uploads with a plain HTTPS PUT
(no AWS credentials on the worker), then reports `artifact.completed`.
Filesystem: the worker writes under the session prefix in the shared
store root (`APIPI_LOCAL_STORE_DIR`) and reports `artifact.completed`
with the session-relative path. Bytes never travel over the socket.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from apipi.common.dirs import store_root
from apipi.common.logutil import RateLimitedLog
from apipi.common.objects import NS_ARTIFACTS, NS_FILES, local_object_path
from apipi.common.store_check import SHARED_STORE_ERROR
from apipi.config import ConfigError, Settings
from apipi.protocol import (
    ArtifactCompletedPayload,
    ArtifactPresignPayload,
    ArtifactPresignReply,
)

_warnings = RateLimitedLog(logging.getLogger("apipi.worker"))


class PresignDisconnected(ConfigError):
    """The socket closed while an upload waited for its presign reply."""


class PresignFuture(asyncio.Future[dict[str, Any]]):
    """A presign waiter that knows which envelope it is waiting on."""

    def __init__(self, session_id: uuid.UUID, *, loop: asyncio.AbstractEventLoop):
        super().__init__(loop=loop)
        self.session_id = session_id
        self.seq = 0


def fail_lost_presign_waiters(
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]], outbox: Any
) -> int:
    """Fail the waiters whose reply was lost with the socket.

    A reply can only be lost once the API acked the `artifact.presign`
    envelope. A waiter whose envelope is still unacked keeps waiting:
    the reconnect replays the envelope and the reply follows.
    """
    failed = 0
    for future in list(waiters.values()):
        if not isinstance(future, PresignFuture) or future.done():
            continue
        if future.seq and outbox.acked_seq(future.session_id) >= future.seq:
            future.set_exception(
                PresignDisconnected("artifact upload failed: worker connection lost")
            )
            failed += 1
    return failed


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def presign_envelope(
    session_id: uuid.UUID,
    *,
    kind: str,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    turn_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, dict[str, Any]]:
    """Build an `artifact.presign` payload for `data` (metadata only)."""
    resolved = request_id if request_id is not None else uuid.uuid4()
    values: dict[str, Any] = {
        "request_id": resolved,
        "kind": kind,
        "filename": filename or "artifact",
        "content_type": content_type or "application/octet-stream",
        "size": len(data),
        "sha256": sha256_hex(data),
    }
    if turn_id is not None:
        values["turn_id"] = turn_id
    return resolved, ArtifactPresignPayload.model_validate(values).to_wire()


def completed_envelope(
    session_id: uuid.UUID,
    *,
    upload_id: uuid.UUID,
    data: bytes,
    path: str | None = None,
    name: str | None = None,
    turn_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build an `artifact.completed` payload (metadata only, no bytes)."""
    payload = ArtifactCompletedPayload(
        upload_id=upload_id, size=len(data), sha256=sha256_hex(data)
    )
    if path is not None:
        payload.path = path
    if name is not None:
        payload.name = name
    if turn_id is not None:
        payload.turn_id = turn_id
    return payload.to_wire()


def write_shared_object(settings: Settings, object_id: str, data: bytes) -> str:
    """Write `data` under the shared store root; return the relative path."""
    from apipi.common.objects import check_object_id as _check_id

    _check_id(object_id)
    root = store_root(settings)
    # File ids are `tenant/file-...` (two parts); session blob keys are
    # `tenant/key/session/blob`. The presign reply carries the exact
    # relative path, so this helper stays for tests and direct callers.
    parts = object_id.strip().strip("/").split("/")
    namespace = NS_FILES if len(parts) == 2 else NS_ARTIFACTS
    path = local_object_path(root, namespace, object_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path.relative_to(root))


def shared_object_path(settings: Settings, local_path: str) -> Path:
    """Resolve a session-relative shared path without escaping the root."""
    from apipi.worker.turn_context import local_ref_path

    return local_ref_path(settings, local_path)


async def put_via_url(
    url: str, data: bytes, headers: dict[str, str] | None = None
) -> None:
    """PUT `data` to a presigned URL with no store credentials."""
    import httpx

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.put(url, content=data, headers=headers or {})
    if response.status_code not in (200, 201, 204):
        raise ConfigError(f"artifact upload failed: HTTP {response.status_code}")


def handle_presign_reply(
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    message: dict[str, Any],
) -> bool:
    """Resolve one `artifact.presign.reply`; True when it matched a waiter."""
    try:
        reply = ArtifactPresignReply.model_validate(message)
    except ValidationError:
        return False
    future = waiters.get(reply.request_id)
    if future is None or future.done():
        return False
    future.set_result(reply.to_wire())
    return True


def write_shared_path(settings: Settings, relative_path: str, data: bytes) -> str:
    """Write `data` to `relative_path` under the shared store root."""
    from apipi.worker.turn_context import local_ref_path

    # Validate the path stays inside the root, then write it.
    target = local_ref_path(settings, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    root = store_root(settings).resolve()
    return str(target.resolve().relative_to(root))


async def upload_via_presign(
    outbox: Any,
    waiters: dict[uuid.UUID, asyncio.Future[dict[str, Any]]],
    settings: Settings,
    session_id: uuid.UUID,
    *,
    kind: str,
    filename: str | None,
    content_type: str | None,
    data: bytes,
    turn_id: uuid.UUID | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Upload one blob without store credentials.

    Appends `artifact.presign` to the outbox, waits for the API reply,
    PUTs (S3) or writes (shared filesystem), then appends
    `artifact.completed`. Quota failures raise today's store errors
    (`artifact_store`, `artifact_too_large`, `workspace_too_large`,
    `payload_too_large`) so the turn fails the way direct writes do.
    """
    from apipi.common.errors import ApiError
    from apipi.config import DiskLimitError

    request_id, presign_payload = presign_envelope(
        session_id,
        kind=kind,
        filename=filename,
        content_type=content_type,
        data=data,
        turn_id=turn_id,
    )
    future = PresignFuture(session_id, loop=asyncio.get_running_loop())
    waiters[request_id] = future
    try:
        envelope = outbox.append(
            session_id,
            "artifact.presign",
            presign_payload,
            turn_id=turn_id,
        )
        future.seq = int(envelope["seq"])
        metrics = getattr(outbox, "metrics", None)
        try:
            reply_message = await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            if metrics is not None:
                metrics.observe_worker_waiter("presign", "timeout")
            _warnings.warning(
                "presign reply timed out",
                event="worker.waiter.timeout",
                error_code="waiter_timeout",
                kind="presign",
                session_id=session_id,
                timeout_seconds=timeout,
            )
            raise ConfigError("artifact upload timed out") from exc
        except PresignDisconnected:
            if metrics is not None:
                metrics.observe_worker_waiter("presign", "disconnected")
            raise
        if metrics is not None:
            metrics.observe_worker_waiter("presign", "ok")
        reply = ArtifactPresignReply.model_validate(reply_message)
        if not reply.ok:
            code = reply.code or "artifact_store"
            message = reply.message or "Cannot write artifacts"
            if code == "payload_too_large":
                raise ApiError("invalid_request", message, code=code, status_code=413)
            raise DiskLimitError(message, code=code)
        if reply.unchanged:
            # The API already holds these bytes; skip the PUT and the
            # completed envelope.
            return {
                "unchanged": True,
                "size": len(data),
                "sha256": sha256_hex(data),
            }
        upload_id = reply.upload_id
        if upload_id is None:
            raise ConfigError("artifact presign reply is missing upload_id")
        if reply.url:
            await put_via_url(reply.url, data, reply.headers)
            completed_path: str | None = None
        else:
            if not reply.path:
                raise ConfigError(SHARED_STORE_ERROR)
            completed_path = write_shared_path(settings, reply.path, data)
        completed = completed_envelope(
            session_id,
            upload_id=upload_id,
            data=data,
            path=completed_path,
            name=filename,
            turn_id=turn_id,
        )
        outbox.append(
            session_id,
            "artifact.completed",
            completed,
            turn_id=turn_id,
        )
        result: dict[str, Any] = {
            "upload_id": str(upload_id),
            "size": len(data),
            "sha256": sha256_hex(data),
        }
        if reply.file_id:
            result["file_id"] = reply.file_id
            result["id"] = reply.file_id
        if reply.artifact_id is not None:
            result["artifact_id"] = str(reply.artifact_id)
        return result
    finally:
        waiters.pop(request_id, None)
