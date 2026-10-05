import contextlib
import dataclasses
import unicodedata
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.errors import ApiError
from apipi.common.objects import NS_FILES
from apipi.config import Settings
from apipi.env.setup import SetupError, file_id_refs_from, inline_files_from
from apipi.gateway.auth import not_found
from apipi.gateway.content import (
    FilePart,
    ImagePart,
    InputFile,
    file_model_input,
    image_mimes,
)
from apipi.gateway.errors import not_implemented
from apipi.store.blobs import ObjectStore, file_object_id
from apipi.store.engine import Store
from apipi.store.models import FileRow, SessionFileRow, utc_now
from apipi.store.repo import (
    bind_session_file,
    clear_paths,
    create_file,
    delete_file,
    delete_unbound_attachments,
    file_cursor,
    get_file,
    list_files,
    list_session_files,
    lock_file,
    lock_session,
    session_file_cursor,
    unbind_files,
)

FILE_PURPOSES = frozenset({"user_data", "assistants", "vision"})
DOCUMENT_PURPOSES = frozenset({"user_data", "assistants"})
FILE_KINDS = ("file", "attachment", "image")
MAX_LIST_LIMIT = 100
DEFAULT_LIST_LIMIT = 20
SWEEP_BATCH = 500
ATTACHMENTS_DIR = "attachments"
MAX_NAME_BYTES = 200
ATTACH_ATTEMPTS = 3
_WORKSPACE_PREFIXES = ("/workspace/", "/tmp/workspace/", "./")
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclasses.dataclass
class Bindings:
    """The session bindings one attach made: new ones, and paths it set."""

    created: list[str] = dataclasses.field(default_factory=list)
    placed: list[str] = dataclasses.field(default_factory=list)


def new_file_id() -> str:
    return f"file-{uuid.uuid4().hex}"


def file_body(row: FileRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "object": "file",
        "bytes": row.size,
        "created_at": int(row.created_at.timestamp()),
        "filename": row.filename,
        "purpose": row.purpose,
        "status": "processed",
    }


def apipi_file_body(row: FileRow) -> dict[str, Any]:
    return {
        **file_body(row),
        "kind": row.kind,
        "user_id": row.user_id,
        "content_type": row.content_type,
    }


def session_file_body(binding: SessionFileRow, row: FileRow) -> dict[str, Any]:
    return {
        "file_id": row.id,
        "kind": row.kind,
        "filename": row.filename,
        "bytes": row.size,
        "content_type": row.content_type,
        "path": binding.path,
        "item_id": str(binding.item_id) if binding.item_id is not None else None,
        "created_at": int(binding.created_at.timestamp()),
    }


def check_image(settings: Settings, content_type: str | None, size: int) -> str:
    """Check an image upload and return its mime type.

    The type must be in `APIPI_IMAGE_MIMES` and the size within
    `APIPI_MAX_IMAGE_BYTES`.
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in image_mimes(settings):
        raise ApiError(
            "invalid_request",
            f"content_type {mime or 'unknown'} is not an allowed image type",
            code="invalid_request",
        )
    if size > settings.max_image_bytes:
        raise ApiError(
            "invalid_request",
            "Image too large",
            code="payload_too_large",
            status_code=413,
        )
    return mime


def check_page(limit: int, order: str) -> None:
    if limit < 1 or limit > MAX_LIST_LIMIT:
        raise ApiError(
            "invalid_request",
            f"limit must be between 1 and {MAX_LIST_LIMIT}",
            code="invalid_request",
        )
    if order not in ("asc", "desc"):
        raise ApiError(
            "invalid_request", "order must be asc or desc", code="invalid_request"
        )


def _unknown_after(after: str) -> None:
    raise ApiError(
        "invalid_request",
        f"after {after} is not a file of this list",
        code="invalid_request",
    )


def page_body(data: list[dict[str, Any]], has_more: bool, key: str) -> dict[str, Any]:
    return {
        "object": "list",
        "data": data,
        "first_id": data[0][key] if data else None,
        "last_id": data[-1][key] if data else None,
        "has_more": has_more,
    }


def attachment_name(filename: str) -> str:
    """A safe file name for `attachments/`: the last path segment.

    Control, format, and line or paragraph separator characters are
    dropped.
    An empty name, `.`, or `..` becomes `file`. A name longer than 200
    bytes is cut and keeps its extension.
    """
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch for ch in name if unicodedata.category(ch) not in _DROPPED_CATEGORIES
    ).strip()
    if name in ("", ".", ".."):
        name = "file"
    if len(name.encode()) <= MAX_NAME_BYTES:
        return name
    stem, dot, extension = name.rpartition(".")
    tail = dot + extension if stem and len(extension.encode()) <= 16 else ""
    head = name[: len(name) - len(tail)]
    while len((head + tail).encode()) > MAX_NAME_BYTES:
        head = head[:-1]
    return head + tail


def free_attachment_path(filename: str, taken: Collection[str]) -> str:
    """`attachments/<name>`, or `attachments/<stem> (n).<ext>` when it is taken."""
    name = attachment_name(filename)
    stem, dot, extension = name.rpartition(".")
    if not stem:
        stem, dot, extension = name, "", ""
    path = f"{ATTACHMENTS_DIR}/{name}"
    number = 2
    while path in taken:
        path = f"{ATTACHMENTS_DIR}/{stem} ({number}){dot}{extension}"
        number += 1
    return path


def _workspace_relative(path: str) -> str:
    text = path.strip()
    for prefix in _WORKSPACE_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


async def _agent_inputs(
    db: AsyncSession, tenant_id: uuid.UUID, environment: dict[str, Any]
) -> tuple[set[str], int]:
    """The workspace paths and the total size of the agent input files.

    The sizes are read tenant-wide: the session already uses these files.
    """
    try:
        inline = inline_files_from(environment)
        refs = file_id_refs_from(environment)
    except SetupError as exc:
        raise ApiError("invalid_request", exc.message, code="invalid_request") from exc
    paths = {_workspace_relative(path) for path, _data in inline}
    total = sum(len(data) for _path, data in inline)
    for path, file_id in refs:
        paths.add(_workspace_relative(path))
        row = await get_file(db, tenant_id, file_id)
        if row is not None:
            total += row.size
    return paths, total


async def attach_session_files(
    db: AsyncSession,
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    files: Sequence[InputFile],
    *,
    environment: dict[str, Any],
    user_id: str | None = None,
) -> tuple[list[InputFile], Bindings]:
    """Bind the `input_file` parts of a session with a computer to their paths.

    Runs in the caller's transaction. The session row and then each file
    row, in `file_id` order, are locked (Postgres), so two messages of one
    session cannot take the same path, and a file the sweep or a delete
    removed is `404`. A file bound to the session with a path keeps that
    path. Any other file gets `attachments/<name>`, or a free name like
    `report (2).xlsx` when the session or an agent input already uses that
    path. Returns the files with their paths, and the bindings this call
    created or gave a path.
    """
    made = Bindings()
    if not files:
        return [], made
    if await lock_session(db, tenant_id, session_id) is None:
        not_found()
    for file_id in sorted({item.file_id for item in files}):
        if await lock_file(db, tenant_id, file_id, user_id=user_id) is None:
            not_found()
    rows, _more = await list_session_files(db, tenant_id, session_id)
    bindings = {binding.file_id: binding for binding, _row in rows}
    sizes = {
        binding.file_id: row.size for binding, row in rows if binding.path is not None
    }
    taken, agent_bytes = await _agent_inputs(db, tenant_id, environment)
    taken.update(binding.path for binding in bindings.values() if binding.path)
    paths: dict[str, str] = {}
    for item in files:
        if item.file_id in paths:
            continue
        binding = bindings.get(item.file_id)
        if binding is not None and binding.path:
            paths[item.file_id] = binding.path
            continue
        path = free_attachment_path(item.filename, taken)
        taken.add(path)
        if binding is None:
            await bind_session_file(db, tenant_id, session_id, item.file_id, path=path)
            made.created.append(item.file_id)
        else:
            binding.path = path
            await db.flush()
            made.placed.append(item.file_id)
        paths[item.file_id] = path
        sizes[item.file_id] = item.size
    total = agent_bytes + sum(sizes.values())
    limit = int(settings.max_workspace_bytes)
    if total > limit:
        raise ApiError(
            "invalid_request",
            f"The agent inputs and the session files need {total} bytes, more "
            f"than the {limit} bytes of the workspace (APIPI_MAX_WORKSPACE_BYTES)",
            code="payload_too_large",
            status_code=413,
        )
    attached = [dataclasses.replace(item, path=paths[item.file_id]) for item in files]
    return attached, made


class FileService:
    def __init__(self, store: Store, objects: ObjectStore, settings: Settings) -> None:
        self.store = store
        self.objects = objects
        self.settings = settings

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        data: bytes,
        filename: str,
        purpose: str,
        content_type: str | None = None,
        kind: str = "file",
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if purpose not in FILE_PURPOSES:
            not_implemented(purpose, f"purpose {purpose} is not implemented")
        if len(data) > int(self.settings.max_file_bytes):
            raise ApiError(
                "invalid_request",
                "File too large",
                code="payload_too_large",
                status_code=413,
            )
        if purpose == "vision":
            check_image(self.settings, content_type, len(data))
            kind = "image"
        name = filename.strip() or "upload"
        file_id = new_file_id()
        await self.objects.put(
            NS_FILES,
            file_object_id(tenant_id, file_id),
            data,
            content_type=content_type,
        )
        async with self.store.session() as db:
            row = await create_file(
                db,
                tenant_id,
                file_id=file_id,
                filename=name,
                purpose=purpose,
                size=len(data),
                content_type=content_type,
                kind=kind,
                user_id=user_id,
            )
            return file_body(row)

    async def image_files(
        self,
        tenant_id: uuid.UUID,
        images: tuple[ImagePart, ...],
        *,
        user_id: str | None = None,
    ) -> dict[str, tuple[str, int]]:
        """Check the `file_id` images and return `{file_id: (mime, size)}`.

        Each must be a file of the tenant that the caller with `user_id`
        can see, with an allowed image type within the image size limit.
        """
        mimes = image_mimes(self.settings)
        known: dict[str, tuple[str, int]] = {}
        async with self.store.session() as db:
            for image in images:
                if not image.file_id or image.file_id in known:
                    continue
                row = await get_file(db, tenant_id, image.file_id, user_id=user_id)
                if row is None:
                    not_found()
                mime = (row.content_type or "").split(";", 1)[0].strip().lower()
                if mime not in mimes:
                    raise ApiError(
                        "invalid_request",
                        f"input_image file {image.file_id} is not an allowed image",
                        code="invalid_request",
                    )
                if row.size > self.settings.max_image_bytes:
                    raise ApiError(
                        "invalid_request",
                        "input_image is too large",
                        code="payload_too_large",
                        status_code=413,
                    )
                known[image.file_id] = (mime, row.size)
        return known

    async def input_files(
        self,
        tenant_id: uuid.UUID,
        files: tuple[FilePart, ...],
        *,
        user_id: str | None = None,
        workspace: bool = False,
    ) -> list[InputFile]:
        """Check the `input_file` parts of a message.

        Each must be a file of the tenant that the caller with `user_id`
        can see. With `workspace` (a session with a computer) every file
        type goes to the workspace, within `APIPI_MAX_FILE_BYTES`.
        Otherwise an allowed image type within `APIPI_MAX_IMAGE_BYTES`
        goes to the model as an image. A text type within
        `APIPI_MAX_INLINE_FILE_BYTES` whose bytes are UTF-8 goes to the
        model as text. Anything else is `unsupported_file_type`.
        """
        if not files:
            return []
        rows: dict[str, FileRow] = {}
        async with self.store.session() as db:
            for part in files:
                if part.file_id in rows:
                    continue
                row = await get_file(db, tenant_id, part.file_id, user_id=user_id)
                if row is None:
                    not_found()
                rows[part.file_id] = row
        if workspace:
            return [self._workspace_file(rows[part.file_id], part) for part in files]
        checked: set[str] = set()
        out: list[InputFile] = []
        for part in files:
            row = rows[part.file_id]
            name = part.filename or row.filename
            mime = (row.content_type or "").split(";", 1)[0].strip().lower()
            model_input = file_model_input(self.settings, mime, name)
            if model_input is None:
                raise ApiError(
                    "invalid_request",
                    f"input_file {name} has the type {mime or 'unknown'}. "
                    "A session needs a computer for this file type.",
                    code="unsupported_file_type",
                )
            limit = (
                self.settings.max_image_bytes
                if model_input == "image"
                else int(self.settings.max_inline_file_bytes)
            )
            if row.size > limit:
                raise ApiError(
                    "invalid_request",
                    f"input_file {name} is {row.size} bytes, more than the "
                    f"{limit} bytes a {model_input} file may have",
                    code="payload_too_large",
                    status_code=413,
                )
            if model_input == "text" and row.id not in checked:
                await self._require_utf8(tenant_id, row.id, name)
                checked.add(row.id)
            out.append(
                InputFile(
                    file_id=row.id,
                    filename=name,
                    mime=mime,
                    size=row.size,
                    model_input=model_input,
                )
            )
        return out

    def _workspace_file(self, row: FileRow, part: FilePart) -> InputFile:
        name = part.filename or row.filename
        limit = int(self.settings.max_file_bytes)
        if row.size > limit:
            raise ApiError(
                "invalid_request",
                f"input_file {name} is {row.size} bytes, more than the {limit} "
                "bytes of APIPI_MAX_FILE_BYTES",
                code="payload_too_large",
                status_code=413,
            )
        return InputFile(
            file_id=row.id,
            filename=name,
            mime=(row.content_type or "").split(";", 1)[0].strip().lower(),
            size=row.size,
            model_input="workspace",
        )

    async def attach(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        files: Sequence[InputFile],
        *,
        environment: dict[str, Any],
        user_id: str | None = None,
    ) -> tuple[list[InputFile], Bindings]:
        """Bind workspace files to a session in one transaction.

        See `attach_session_files`. A unique path conflict with another
        transaction is tried again.
        """
        for attempt in range(ATTACH_ATTEMPTS):
            try:
                async with self.store.session() as db:
                    return await attach_session_files(
                        db,
                        self.settings,
                        tenant_id,
                        session_id,
                        files,
                        environment=environment,
                        user_id=user_id,
                    )
            except IntegrityError:
                if attempt + 1 == ATTACH_ATTEMPTS:
                    raise
        return [], Bindings()

    async def unbind(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID, made: Bindings
    ) -> None:
        """Undo the bindings of a turn that did not start, best effort.

        New bindings are deleted, and a path set on an older binding is
        cleared again.
        """
        if made.created or made.placed:
            with contextlib.suppress(Exception):
                async with self.store.session() as db:
                    await unbind_files(db, tenant_id, session_id, made.created)
                    await clear_paths(db, tenant_id, session_id, made.placed)
        made.created.clear()
        made.placed.clear()

    async def _require_utf8(
        self, tenant_id: uuid.UUID, file_id: str, name: str
    ) -> None:
        data = await self.objects.get(NS_FILES, file_object_id(tenant_id, file_id))
        if data is None:
            not_found()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(
                "invalid_request",
                f"input_file {name} is not UTF-8 text. "
                "A session needs a computer for this file.",
                code="unsupported_file_type",
            ) from exc

    async def input_images(
        self,
        tenant_id: uuid.UUID,
        images: tuple[ImagePart, ...],
        known: dict[str, tuple[str, int]],
        *,
        user_id: str | None = None,
    ) -> list[tuple[str, str, int]]:
        """Return `(file_id, mime, size)` per image.

        Data URL images are stored as files of kind `image` owned by
        `user_id`, the user of the session.
        """
        out: list[tuple[str, str, int]] = []
        for image in images:
            if image.file_id:
                mime, size = known[image.file_id]
                out.append((image.file_id, mime, size))
                continue
            created = await self.create(
                tenant_id,
                data=image.data,
                filename="image",
                purpose="user_data",
                content_type=image.mime,
                kind="image",
                user_id=user_id,
            )
            out.append((str(created["id"]), image.mime, len(image.data)))
        return out

    async def list_objects(
        self,
        tenant_id: uuid.UUID,
        *,
        kinds: Collection[str] | None = ("file",),
        purpose: str | None = None,
        owner_id: str | None = None,
        session_id: uuid.UUID | None = None,
        filename_prefix: str | None = None,
        ids: Collection[str] | None = None,
        after: str | None = None,
        order: str = "desc",
        limit: int = DEFAULT_LIST_LIMIT,
        apipi: bool = False,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of the files the caller with `user_id` can see.

        `ids` is the authorization filter and `owner_id` filters by the
        file's `user_id`. `kinds` of None lists every kind. `apipi` adds
        `kind`, `user_id`, and `content_type` to each object.
        """
        check_page(limit, order)
        if kinds is not None:
            unknown = sorted(set(kinds) - set(FILE_KINDS))
            if unknown:
                raise ApiError(
                    "invalid_request",
                    f"kind {unknown[0]} is not file, attachment, or image",
                    code="invalid_request",
                )
        async with self.store.session() as db:
            cursor: tuple[datetime, str] | None = None
            if after is not None:
                cursor = await file_cursor(db, tenant_id, after, user_id=user_id)
                if cursor is None or (ids is not None and after not in ids):
                    _unknown_after(after)
            rows, has_more = await list_files(
                db,
                tenant_id,
                kinds=kinds,
                purpose=purpose,
                owner_id=owner_id,
                session_id=session_id,
                filename_prefix=filename_prefix,
                ids=ids,
                after=cursor,
                order=order,
                limit=limit,
                user_id=user_id,
            )
        body = apipi_file_body if apipi else file_body
        return page_body([body(row) for row in rows], has_more, "id")

    async def list_session_files(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        ids: Collection[str] | None = None,
        after: str | None = None,
        order: str = "desc",
        limit: int = DEFAULT_LIST_LIMIT,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """One page of the files bound to a session the caller has checked."""
        check_page(limit, order)
        async with self.store.session() as db:
            cursor: tuple[datetime, str] | None = None
            if after is not None:
                cursor = await session_file_cursor(
                    db, tenant_id, session_id, after, user_id=user_id
                )
                if cursor is None or (ids is not None and after not in ids):
                    _unknown_after(after)
            rows, has_more = await list_session_files(
                db,
                tenant_id,
                session_id,
                ids=ids,
                after=cursor,
                order=order,
                limit=limit,
                user_id=user_id,
            )
        data = [session_file_body(binding, row) for binding, row in rows]
        return page_body(data, has_more, "file_id")

    async def bind_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        file_ids: Collection[str],
        *,
        path: str | None = None,
        item_id: uuid.UUID | None = None,
    ) -> None:
        """Bind files of the tenant to a session.

        A file already bound to the session keeps its binding. `path` is
        the workspace path of an attachment.
        """
        async with self.store.session() as db:
            for file_id in dict.fromkeys(file_ids):
                await bind_session_file(
                    db, tenant_id, session_id, file_id, path=path, item_id=item_id
                )

    async def bound_files(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[tuple[SessionFileRow, FileRow]]:
        """Every file bound to a session with its binding, oldest first."""
        async with self.store.session() as db:
            rows, _more = await list_session_files(
                db, tenant_id, session_id, order="asc"
            )
        return rows

    async def delete_bytes(self, tenant_id: uuid.UUID, file_ids: list[str]) -> None:
        """Delete stored file bytes after their rows are gone, best effort."""
        for file_id in file_ids:
            with contextlib.suppress(Exception):
                await self.objects.delete(NS_FILES, file_object_id(tenant_id, file_id))

    async def sweep_attachments(self, now: datetime | None = None) -> int:
        """Delete unbound attachments older than `APIPI_ATTACHMENT_TTL`.

        Returns the number of deleted files.
        """
        before = (now if now is not None else utc_now()) - self.settings.attachment_ttl
        total = 0
        while True:
            async with self.store.session() as db:
                deleted = await delete_unbound_attachments(
                    db, before=before, limit=SWEEP_BATCH
                )
            for tenant_id, file_id in deleted:
                await self.delete_bytes(tenant_id, [file_id])
            total += len(deleted)
            if len(deleted) < SWEEP_BATCH:
                return total

    async def meta(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> tuple[str, str | None]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        return row.filename, row.content_type

    async def get(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        return file_body(row)

    async def content(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> tuple[bytes, str | None, str]:
        async with self.store.session() as db:
            row = await get_file(db, tenant_id, file_id, user_id=user_id)
        if row is None:
            not_found()
        data = await self.objects.get(NS_FILES, file_object_id(tenant_id, file_id))
        if data is None:
            not_found()
        return data, row.content_type, row.filename

    async def delete(
        self, tenant_id: uuid.UUID, file_id: str, *, user_id: str | None = None
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if not await delete_file(db, tenant_id, file_id, user_id=user_id):
                not_found()
        await self.objects.delete(NS_FILES, file_object_id(tenant_id, file_id))
        return {"id": file_id, "object": "file", "deleted": True}

    async def workspace_files(
        self,
        tenant_id: uuid.UUID,
        environment: dict[str, Any],
        *,
        user_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        try:
            refs = file_id_refs_from(environment)
        except SetupError as exc:
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        if not refs:
            return []
        files: list[tuple[str, bytes]] = []
        async with self.store.session() as db:
            for path, file_id in refs:
                row = await get_file(db, tenant_id, file_id, user_id=user_id)
                if row is None:
                    not_found()
                data = await self.objects.get(
                    NS_FILES, file_object_id(tenant_id, file_id)
                )
                if data is None:
                    not_found()
                files.append((path, data))
        return files
