"""API-issued artifact uploads for workers (protocol v2, step 6/8).

The worker never holds object-store credentials. It sends
`artifact.presign` (metadata only) over the durable outbox, the API
checks quotas and binds a key under the session prefix, and the worker
uploads directly (S3 presigned PUT) or writes to the shared store root
(filesystem). It then sends `artifact.completed` and the API verifies
the object before writing artifact, file, or Pi session rows.

Bytes never travel over the worker socket: both messages carry only
ids, paths, sizes, and checksums. The 1 MiB durable envelope cap in
`apipi.worker.protocol` enforces that alongside these schemas.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import ConfigError, DiskLimitError, Settings
from apipi.store.blobs import (
    NS_ARTIFACTS,
    NS_FILES,
    Namespace,
    ObjectStore,
    ObjectStoreError,
    blob_key,
    blob_prefix,
    file_object_id,
    local_object_path,
    object_store,
)
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import (
    create_artifact,
    create_artifact_upload,
    get_artifact_upload,
    get_session,
)
from apipi.worker.pi.dirs import store_root

ARTIFACT_KINDS = frozenset({"artifact", "pi_session", "input_image"})

SHARED_STORE_ERROR = (
    "filesystem store requires a shared path: mount the same "
    "APIPI_LOCAL_STORE_DIR on the API and every worker"
)

PRESIGN_TTL = timedelta(minutes=15)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_error(message: str, *, operation: str, key: str = "") -> ObjectStoreError:
    return ObjectStoreError(
        message, operation=operation, bucket="", key=key, code="artifact_store"
    )


def check_artifact_kind(kind: str) -> str:
    if kind not in ARTIFACT_KINDS:
        raise _store_error(f"unknown artifact kind: {kind}", operation="presign")
    return kind


def check_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise _store_error("invalid sha256", operation="presign", key=text)
    return text


def session_object_id(
    tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID, artifact_id: uuid.UUID
) -> str:
    return blob_key(tenant_id, key_id, session_id, artifact_id)


def session_prefix(tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID) -> str:
    return blob_prefix(tenant_id, key_id, session_id)


def check_session_prefix(object_id: str, prefix: str) -> None:
    if not object_id.startswith(prefix):
        raise _store_error(
            "artifact is outside the session prefix",
            operation="complete",
            key=object_id,
        )


def check_completed_path(path: str, *, prefix_parts: tuple[str, ...] = ()) -> str:
    """Validate a filesystem `artifact.completed` relative path.

    The path must stay inside the session prefix and must not escape
    with `..`. Absolute paths are rejected.
    """
    del prefix_parts
    if not isinstance(path, str) or not path.strip():
        raise _store_error(
            "artifact path is required", operation="complete", key=path or ""
        )
    if path.strip().startswith("/"):
        raise _store_error(
            "artifact path escapes the session prefix",
            operation="complete",
            key=path,
        )
    text = path.strip().strip("/")
    if not text:
        raise _store_error(
            "artifact path is required", operation="complete", key=path or ""
        )
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _store_error(
            "artifact path escapes the session prefix",
            operation="complete",
            key=path,
        )
    return text


def _quota_for_kind(settings: Settings, kind: str, declared: int) -> None:
    if declared < 1:
        raise DiskLimitError("Artifact is empty", code="artifact_store")
    if kind == "input_image":
        if declared > int(settings.max_file_bytes):
            from apipi.gateway.errors import ApiError

            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        return
    if declared > int(settings.max_workspace_bytes):
        raise DiskLimitError("Workspace too large", code="workspace_too_large")
    if declared > int(settings.max_artifact_bytes):
        raise DiskLimitError("Artifact store too large", code="artifact_too_large")


async def check_quota(
    db: AsyncSession,
    settings: Settings,
    row: SessionRow,
    *,
    kind: str,
    declared: int,
    blobs_used_bytes: int | None = None,
) -> None:
    _quota_for_kind(settings, kind, declared)
    if kind == "input_image":
        return
    used = blobs_used_bytes
    if used is None:
        used = 0
    cache = row.pi_session_bytes if row is not None else 0
    if used - cache + declared > int(settings.max_artifact_bytes):
        raise DiskLimitError("Artifact store too large", code="artifact_too_large")


async def issue_artifact_presign(
    db: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    kind: str,
    filename: str | None,
    content_type: str | None,
    size: int,
    sha256: str | None,
    key_id: str,
    used_bytes: int,
    objects: ObjectStore | None = None,
) -> dict[str, Any]:
    """Reserve one upload slot; S3 also mints the presigned PUT URL."""
    kind = check_artifact_kind(kind)
    digest = check_sha256(sha256)
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        raise _store_error("unknown session", operation="presign")
    await check_quota(
        db, settings, row, kind=kind, declared=size, blobs_used_bytes=used_bytes
    )
    artifact_id = uuid.uuid4()
    name = (filename or "").strip() or (
        "pi-session.jsonl" if kind == "pi_session" else "artifact"
    )
    ctype = (content_type or "").strip() or "application/octet-stream"
    expires_at = utc_now() + settings.presign_ttl
    upload = await create_artifact_upload(
        db,
        tenant_id,
        session_id=session_id,
        artifact_id=artifact_id,
        kind=kind,
        filename=name,
        content_type=ctype,
        declared_bytes=size,
        sha256=digest,
        expires_at=expires_at,
    )
    if kind == "input_image":
        file_id = f"file-{artifact_id.hex}"
        namespace: Namespace = NS_FILES
        object_id = file_object_id(tenant_id, file_id)
    else:
        file_id = None
        namespace = NS_ARTIFACTS
        object_id = session_object_id(tenant_id, key_id, session_id, artifact_id)
    relative_path: str | None = None
    url: str | None = None
    headers: dict[str, str] = {}
    if settings.artifact_store == "s3":
        backend = objects if objects is not None else object_store(settings)
        presign = getattr(backend, "presign", None)
        if presign is None:
            raise _store_error(
                "object store cannot presign PUT URLs", operation="presign"
            )
        url, headers = presign(
            "PUT",
            namespace,
            object_id,
            expires=settings.presign_ttl,
            content_type=ctype,
        )
        headers = dict(headers)
    else:
        root = store_root(settings)
        relative_path = str(
            local_object_path(root, namespace, object_id).relative_to(root)
        )
    return {
        "upload_id": upload.id,
        "artifact_id": artifact_id,
        "object_id": object_id,
        "namespace": namespace,
        "file_id": file_id,
        "path": relative_path,
        "url": url,
        "headers": headers,
        "expires_at": expires_at.isoformat(),
    }


async def _read_s3_object(
    backend: Any, namespace: Namespace, object_id: str
) -> bytes | None:
    get = getattr(backend, "get", None)
    if get is None:
        return None
    return await get(namespace, object_id)


async def complete_artifact_upload(
    db: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    upload_id: uuid.UUID,
    size: int | None,
    sha256: str | None,
    path: str | None = None,
    name: str | None = None,
    turn_id: uuid.UUID | None = None,
    key_id: str,
    objects: ObjectStore | None = None,
) -> dict[str, Any]:
    """Verify one upload and record its rows. Rejects foreign ids/paths."""
    from apipi.store.repo import create_file

    upload = await get_artifact_upload(db, tenant_id, upload_id)
    if upload is None or upload.session_id != session_id:
        raise _store_error(
            "unknown upload for this session",
            operation="complete",
            key=str(upload_id),
        )
    if upload.status == "complete":
        result: dict[str, Any] = {
            "upload_id": str(upload.id),
            "artifact_id": str(upload.artifact_id),
        }
        if upload.kind == "input_image":
            result["file_id"] = f"file-{upload.artifact_id.hex}"
        return result
    if utc_now() > _aware(upload.expires_at):
        raise _store_error("upload URL expired", operation="complete")
    digest = check_sha256(sha256)
    if upload.kind == "input_image":
        file_id = f"file-{upload.artifact_id.hex}"
        object_id: str = file_object_id(tenant_id, file_id)
        namespace: Namespace = NS_FILES
    else:
        file_id = None
        object_id = session_object_id(tenant_id, key_id, session_id, upload.artifact_id)
        namespace = NS_ARTIFACTS
        check_session_prefix(object_id, session_prefix(tenant_id, key_id, session_id))
    if settings.artifact_store == "s3":
        if path is not None:
            raise _store_error(
                "filesystem path is not valid with the S3 store",
                operation="complete",
                key=path,
            )
        backend = objects if objects is not None else object_store(settings)
        head = getattr(backend, "head", None)
        if head is None:
            raise _store_error("object store cannot verify uploads", operation="head")
        meta = await head(namespace, object_id)
        if meta is None:
            raise _store_error(
                "Object is missing; PUT the presigned URL first",
                operation="complete",
                key=object_id,
            )
        actual_size = meta[0] if isinstance(meta, tuple) else None
        if actual_size is None:
            raise _store_error("cannot verify upload size", operation="head")
        if size is not None and size != actual_size:
            raise _store_error(
                "upload size mismatch", operation="complete", key=object_id
            )
        if actual_size != upload.declared_bytes and size is None:
            raise _store_error(
                "upload size mismatch", operation="complete", key=object_id
            )
        if digest is not None or upload.sha256 is not None:
            data = await _read_s3_object(backend, namespace, object_id)
            if data is None:
                raise _store_error(
                    "Object is missing; PUT the presigned URL first",
                    operation="complete",
                    key=object_id,
                )
            actual_digest = sha256_hex(data)
            expected = digest if digest is not None else upload.sha256
            if expected is not None and actual_digest != expected.lower():
                raise _store_error(
                    "upload checksum mismatch", operation="complete", key=object_id
                )
            actual_size = len(data)
    else:
        if path is None:
            raise _store_error(
                "filesystem upload needs a session-relative path",
                operation="complete",
            )
        relative = check_completed_path(path)
        root = store_root(settings)
        expected_rel = local_object_path(root, namespace, object_id).relative_to(root)
        if relative != str(expected_rel):
            raise _store_error(
                "artifact path is outside the session prefix",
                operation="complete",
                key=path,
            )
        candidate = (root / relative).resolve()
        resolved_root = root.resolve()
        if candidate != resolved_root and resolved_root not in candidate.parents:
            raise _store_error(
                "artifact path escapes the store root",
                operation="complete",
                key=path,
            )
        if not candidate.is_file():
            raise _store_error(
                "uploaded file is missing", operation="complete", key=path
            )
        data = candidate.read_bytes()
        actual_size = len(data)
        if size is not None and size != actual_size:
            raise _store_error("upload size mismatch", operation="complete", key=path)
        actual_digest = sha256_hex(data)
        expected = digest if digest is not None else upload.sha256
        if expected is not None and actual_digest != expected.lower():
            raise _store_error(
                "upload checksum mismatch", operation="complete", key=path
            )
    if upload.kind == "pi_session":
        row = await get_session(db, tenant_id, session_id)
        if row is None:
            raise _store_error("unknown session", operation="complete")
        row.pi_session_id = upload.artifact_id
        row.pi_session_bytes = actual_size
        await db.flush()
    elif upload.kind == "input_image":
        assert file_id is not None
        artifact_name = (name or upload.filename).strip() or upload.filename
        await create_file(
            db,
            tenant_id,
            file_id=file_id,
            filename=artifact_name,
            purpose="user_data",
            size=actual_size,
            content_type=upload.content_type,
        )
    else:
        artifact_name = (name or upload.filename).strip() or upload.filename
        await create_artifact(
            db,
            tenant_id,
            session_id,
            path=artifact_name,
            content_type=upload.content_type,
            turn_id=turn_id,
            key_id=key_id,
            byte_size=actual_size,
            artifact_id=upload.artifact_id,
        )
    upload.status = "complete"
    await db.flush()
    done: dict[str, Any] = {
        "upload_id": str(upload.id),
        "artifact_id": str(upload.artifact_id),
    }
    if file_id is not None:
        done["file_id"] = file_id
    return done


def _aware(value: Any) -> Any:
    from datetime import UTC

    if hasattr(value, "tzinfo") and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# --- Shared store root proof -------------------------------------------------


def write_store_check(root: Path) -> tuple[str, str]:
    """Write a nonce marker file; the worker must read it back."""
    nonce = secrets.token_hex(16)
    marker = f".apipi-store-check-{nonce}"
    path = root / marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(nonce, encoding="utf-8")
    return marker, nonce


def read_store_check(root: Path, marker: str, nonce: str) -> bool:
    """True when `marker` under `root` contains exactly `nonce`."""
    name = (marker or "").strip().strip("/")
    if not name or "/" in name or name in {".", ".."}:
        return False
    if not name.startswith(".apipi-store-check-"):
        return False
    try:
        text = (root / name).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(nonce) and text == nonce


def verify_store_proof(root: Path, marker: str, nonce: str) -> None:
    if not read_store_check(root, marker, nonce):
        raise ConfigError(SHARED_STORE_ERROR)


def shared_store_required(settings: Settings) -> bool:
    return settings.artifact_store == "local"
