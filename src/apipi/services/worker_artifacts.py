"""API-issued artifact uploads for workers (protocol v2, step 6/8).

The worker never holds object-store credentials. It sends
`artifact.presign` (metadata only) over the durable outbox, the API
checks quotas and binds a key under the session prefix, and the worker
uploads directly (S3 presigned PUT) or writes to the shared store root
(filesystem). It then sends `artifact.completed` and the API verifies
the object before writing artifact or Pi session rows.

Bytes never travel over the worker socket: both messages carry only
ids, paths, sizes, and checksums. The 1 MiB durable envelope cap in
`apipi.protocol` enforces that alongside these schemas.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.dirs import store_root
from apipi.common.errors import ObjectStoreError
from apipi.common.objects import NS_ARTIFACTS, Namespace, local_object_path
from apipi.common.timefmt import utc_ts
from apipi.config import DiskLimitError, Settings
from apipi.store.blobs import (
    ArtifactBlobs,
    ObjectHead,
    ObjectStore,
    blob_key,
    blob_prefix,
    hash_stream,
    object_store,
)
from apipi.store.engine import Store
from apipi.store.models import ArtifactUploadRow, SessionRow, utc_now
from apipi.store.repo import (
    create_artifact,
    create_artifact_upload,
    get_artifact_upload,
    get_artifact_upload_by_request,
    get_session,
    list_artifacts,
)

ARTIFACT_KINDS = frozenset({"artifact", "pi_session"})

REMOVED_KINDS = {
    "input_image": "artifact kind input_image is not supported; "
    "upgrade the worker to 0.15.0 or later",
}

PRESIGN_TTL = timedelta(minutes=15)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_error(message: str, *, operation: str, key: str = "") -> ObjectStoreError:
    return ObjectStoreError(
        message, operation=operation, bucket="", key=key, code="artifact_store"
    )


def check_artifact_kind(kind: str, *, operation: str = "presign") -> str:
    if kind in REMOVED_KINDS:
        raise _store_error(REMOVED_KINDS[kind], operation=operation)
    if kind not in ARTIFACT_KINDS:
        raise _store_error(f"unknown artifact kind: {kind}", operation=operation)
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


def _check_declared(settings: Settings, declared: int) -> None:
    if declared < 1:
        raise DiskLimitError("Artifact is empty", code="artifact_store")
    if declared > int(settings.max_workspace_bytes):
        raise DiskLimitError("Workspace too large", code="workspace_too_large")
    if declared > int(settings.max_artifact_bytes):
        raise DiskLimitError("Artifact store too large", code="artifact_too_large")


async def check_quota(
    db: AsyncSession,
    settings: Settings,
    row: SessionRow,
    *,
    declared: int,
    blobs_used_bytes: int | None = None,
) -> None:
    _check_declared(settings, declared)
    used = blobs_used_bytes
    if used is None:
        used = 0
    cache = row.pi_session_bytes if row is not None else 0
    if used - cache + declared > int(settings.max_artifact_bytes):
        raise DiskLimitError("Artifact store too large", code="artifact_too_large")


def presign_name(kind: str, filename: str | None) -> str:
    return (filename or "").strip() or (
        "pi-session.jsonl" if kind == "pi_session" else "artifact"
    )


def artifact_name(upload: ArtifactUploadRow, name: str | None) -> str:
    return (name or upload.filename).strip() or upload.filename


async def _digest_blob(
    blobs: ArtifactBlobs,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> str | None:
    digest = getattr(blobs, "digest", None)
    if digest is not None:
        found = await digest(tenant_id, key_id, session_id, artifact_id)
        return found[1] if found is not None else None
    data = await blobs.get(tenant_id, key_id, session_id, artifact_id)
    if data is None:
        return None
    return await asyncio.to_thread(sha256_hex, data)


async def latest_artifact_matches(
    existing: list[Any],
    blobs: ArtifactBlobs,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    path: str,
    digest: str,
) -> bool:
    """True when the latest artifact at `path` already holds `digest`.

    `existing` is the session's artifact list. The stored bytes are
    hashed in chunks off the event loop."""
    for artifact in reversed(existing):
        if artifact.path != path:
            continue
        actual = await _digest_blob(
            blobs, tenant_id, artifact.key_id or key_id, session_id, artifact.id
        )
        return actual is not None and actual == digest.lower()
    return False


async def _latest_artifact_matches(
    db: AsyncSession,
    blobs: ArtifactBlobs,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    path: str,
    digest: str,
) -> bool:
    existing = await list_artifacts(db, tenant_id, session_id) or []
    return await latest_artifact_matches(
        existing, blobs, tenant_id, key_id, session_id, path, digest
    )


async def precheck_presign(
    store: Store,
    blobs: ArtifactBlobs,
    row: SessionRow,
    *,
    kind: str,
    filename: str | None,
    sha256: str | None,
) -> tuple[int, bool | None]:
    """The object-store reads of one presign, done before any row lock.

    Returns the bytes the session already stores and, for a plain
    artifact with a digest, whether the latest stored bytes at that
    path already match it. No database transaction is open while the
    store is read."""
    kind = check_artifact_kind(kind)
    digest = check_sha256(sha256)
    used = await blobs.used_bytes(row.tenant_id, row.key_id, row.id)
    unchanged: bool | None = None
    if kind == "artifact" and digest is not None:
        async with store.session() as db:
            existing = await list_artifacts(db, row.tenant_id, row.id) or []
        unchanged = await latest_artifact_matches(
            existing,
            blobs,
            row.tenant_id,
            row.key_id,
            row.id,
            presign_name(kind, filename),
            digest,
        )
    return used, unchanged


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
    blobs: ArtifactBlobs | None = None,
    unchanged: bool | None = None,
    request_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Reserve one upload slot; S3 also mints the presigned PUT URL.

    `unchanged` is the answer of `precheck_presign` when the caller read
    the store before taking the row lock; without it the check runs here.
    The slot is stored with the worker's `request_id`. A second call with
    the same `request_id` (a replayed envelope) answers from that slot
    and reserves nothing new, so the reply is always the same.
    """
    kind = check_artifact_kind(kind)
    digest = check_sha256(sha256)
    row = await get_session(db, tenant_id, session_id)
    if row is None:
        raise _store_error("unknown session", operation="presign")
    name = presign_name(kind, filename)
    existing_upload = (
        await get_artifact_upload_by_request(db, tenant_id, session_id, request_id)
        if request_id is not None
        else None
    )
    if existing_upload is not None:
        unchanged = False
    if unchanged is None and (
        kind == "artifact" and digest is not None and blobs is not None
    ):
        unchanged = await _latest_artifact_matches(
            db, blobs, tenant_id, row.key_id, session_id, name, digest
        )
    if unchanged:
        # Skip files whose latest stored bytes already match; answer
        # `unchanged` so the worker skips the PUT and no upload slot is
        # reserved. This check runs before quota so unchanged files never
        # trip the store limits.
        return {
            "unchanged": True,
            "upload_id": None,
            "artifact_id": None,
            "object_id": None,
            "namespace": NS_ARTIFACTS,
            "path": None,
            "url": None,
            "headers": {},
            "expires_at": None,
        }
    if existing_upload is not None:
        upload = existing_upload
        artifact_id = upload.artifact_id
        ctype = upload.content_type
        expires_at = _aware(upload.expires_at)
        if expires_at <= utc_now():
            expires_at = utc_now() + settings.presign_ttl
            upload.expires_at = expires_at
            await db.flush()
    else:
        await check_quota(db, settings, row, declared=size, blobs_used_bytes=used_bytes)
        if kind == "pi_session" and row.pi_session_id is not None:
            # Reuse the blob id so every save overwrites the same object
            # instead of leaking one new object per save.
            artifact_id = row.pi_session_id
        else:
            artifact_id = uuid.uuid4()
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
            request_id=request_id,
        )
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
            NS_ARTIFACTS,
            object_id,
            expires=settings.presign_ttl,
            content_type=ctype,
        )
        headers = dict(headers)
    else:
        root = store_root(settings)
        relative_path = str(
            local_object_path(root, NS_ARTIFACTS, object_id).relative_to(root)
        )
    return {
        "upload_id": upload.id,
        "artifact_id": artifact_id,
        "object_id": object_id,
        "namespace": NS_ARTIFACTS,
        "path": relative_path,
        "url": url,
        "headers": headers,
        "expires_at": utc_ts(expires_at),
    }


async def _read_s3_object(
    backend: Any, namespace: Namespace, object_id: str
) -> bytes | None:
    get = getattr(backend, "get", None)
    if get is None:
        return None
    return await get(namespace, object_id)


async def _digest_object(
    backend: Any, namespace: Namespace, object_id: str
) -> tuple[int, str] | None:
    digest = getattr(backend, "digest", None)
    if digest is not None:
        return await digest(namespace, object_id)
    data = await _read_s3_object(backend, namespace, object_id)
    if data is None:
        return None
    return len(data), await asyncio.to_thread(sha256_hex, data)


def _upload_target(
    upload: ArtifactUploadRow, tenant_id: uuid.UUID, session_id: uuid.UUID, key_id: str
) -> str:
    check_artifact_kind(upload.kind, operation="complete")
    object_id = session_object_id(tenant_id, key_id, session_id, upload.artifact_id)
    check_session_prefix(object_id, session_prefix(tenant_id, key_id, session_id))
    return object_id


def _check_local_object(
    settings: Settings,
    namespace: Namespace,
    object_id: str,
    path: str | None,
) -> tuple[int, str]:
    """Check a filesystem upload and hash it in chunks. Runs in a thread."""
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
        raise _store_error("uploaded file is missing", operation="complete", key=path)
    with candidate.open("rb") as handle:
        return hash_stream(handle.read)


async def verify_upload_object(
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    upload: ArtifactUploadRow,
    *,
    size: int | None,
    sha256: str | None,
    path: str | None,
    key_id: str,
    objects: ObjectStore | None = None,
) -> int:
    """Check the uploaded object against the declared size and checksum.

    This is every object-store or filesystem read of an upload: S3
    `HEAD`, a chunked hash, or a filesystem hash in a thread. It touches
    no database row, so the caller runs it before taking a row lock.
    Returns the verified size.
    """
    digest = check_sha256(sha256)
    object_id = _upload_target(upload, tenant_id, session_id, key_id)
    expected = digest if digest is not None else upload.sha256
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
        meta = await head(NS_ARTIFACTS, object_id)
        if meta is None:
            raise _store_error(
                "Object is missing; PUT the presigned URL first",
                operation="complete",
                key=object_id,
            )
        actual_size = meta.size if isinstance(meta, ObjectHead) else None
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
        if expected is not None:
            found = await _digest_object(backend, NS_ARTIFACTS, object_id)
            if found is None:
                raise _store_error(
                    "Object is missing; PUT the presigned URL first",
                    operation="complete",
                    key=object_id,
                )
            actual_size, actual_digest = found
            if actual_digest != expected.lower():
                raise _store_error(
                    "upload checksum mismatch", operation="complete", key=object_id
                )
        return actual_size
    actual_size, actual_digest = await asyncio.to_thread(
        _check_local_object, settings, NS_ARTIFACTS, object_id, path
    )
    if size is not None and size != actual_size:
        raise _store_error("upload size mismatch", operation="complete", key=path or "")
    if expected is not None and actual_digest != expected.lower():
        raise _store_error(
            "upload checksum mismatch", operation="complete", key=path or ""
        )
    return actual_size


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
    verified_size: int | None = None,
) -> dict[str, Any]:
    """Verify one upload and record its rows. Rejects foreign ids/paths.

    `verified_size` is the result of `verify_upload_object` when the
    caller already checked the object outside the row lock; without it
    the check runs here."""
    upload = await get_artifact_upload(db, tenant_id, upload_id)
    if upload is None or upload.session_id != session_id:
        raise _store_error(
            "unknown upload for this session",
            operation="complete",
            key=str(upload_id),
        )
    if upload.status == "complete":
        return {
            "upload_id": str(upload.id),
            "artifact_id": str(upload.artifact_id),
        }
    if utc_now() > _aware(upload.expires_at):
        raise _store_error("upload URL expired", operation="complete")
    _upload_target(upload, tenant_id, session_id, key_id)
    if verified_size is None:
        actual_size = await verify_upload_object(
            settings,
            tenant_id,
            session_id,
            upload,
            size=size,
            sha256=sha256,
            path=path,
            key_id=key_id,
            objects=objects,
        )
    else:
        check_sha256(sha256)
        actual_size = verified_size
    if upload.kind == "pi_session":
        row = await get_session(db, tenant_id, session_id)
        if row is None:
            raise _store_error("unknown session", operation="complete")
        row.pi_session_id = upload.artifact_id
        row.pi_session_bytes = actual_size
        await db.flush()
    else:
        await create_artifact(
            db,
            tenant_id,
            session_id,
            path=artifact_name(upload, name),
            content_type=upload.content_type,
            turn_id=turn_id,
            key_id=key_id,
            byte_size=actual_size,
            artifact_id=upload.artifact_id,
        )
    upload.status = "complete"
    await db.flush()
    return {
        "upload_id": str(upload.id),
        "artifact_id": str(upload.artifact_id),
    }


def _aware(value: Any) -> Any:
    from datetime import UTC

    if hasattr(value, "tzinfo") and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


# --- Shared store root proof -------------------------------------------------


def shared_store_required(settings: Settings) -> bool:
    return settings.artifact_store == "local"


async def wipe_artifact_store(
    blobs: ArtifactBlobs,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
) -> None:
    await blobs.delete_session(tenant_id, key_id, session_id)
