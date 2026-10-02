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
import uuid
from pathlib import Path
from typing import Any

from apipi.config import ConfigError, Settings
from apipi.store.blobs import NS_ARTIFACTS, local_object_path
from apipi.worker.pi.dirs import store_root


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
    payload: dict[str, Any] = {
        "request_id": str(resolved),
        "kind": kind,
        "filename": filename or "artifact",
        "content_type": content_type or "application/octet-stream",
        "size": len(data),
        "sha256": sha256_hex(data),
    }
    if turn_id is not None:
        payload["turn_id"] = str(turn_id)
    return resolved, payload


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
    payload: dict[str, Any] = {
        "upload_id": str(upload_id),
        "size": len(data),
        "sha256": sha256_hex(data),
    }
    if path is not None:
        payload["path"] = path
    if name is not None:
        payload["name"] = name
    if turn_id is not None:
        payload["turn_id"] = str(turn_id)
    return payload


def write_shared_object(settings: Settings, object_id: str, data: bytes) -> str:
    """Write `data` under the shared store root; return the relative path."""
    from apipi.store.blobs import _object_id as _check_id

    _check_id(object_id)
    root = store_root(settings)
    path = local_object_path(root, NS_ARTIFACTS, object_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path.relative_to(root))


def shared_object_path(settings: Settings, local_path: str) -> Path:
    """Resolve a session-relative shared path without escaping the root."""
    from apipi.services.turn_context import local_ref_path

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
    if message.get("type") != "artifact.presign.reply":
        return False
    raw = message.get("request_id")
    try:
        request_id = uuid.UUID(str(raw))
    except (ValueError, TypeError):
        return False
    future = waiters.get(request_id)
    if future is None or future.done():
        return False
    future.set_result(message)
    return True
