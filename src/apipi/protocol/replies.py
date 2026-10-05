"""API to worker replies: the cumulative ack, presign replies, search."""

import uuid
from typing import Literal

from pydantic import Field

from apipi.protocol.base import ControlMessage


class CumulativeAck(ControlMessage):
    """API to worker. Cumulative ack: everything up to `last_seq` is persisted."""

    type: Literal["ack"] = "ack"
    session_id: uuid.UUID
    last_seq: int = Field(ge=0)


class ArtifactPresignReply(ControlMessage):
    """API to worker. Answer to one `artifact.presign` envelope.

    S3 carries `url`, `headers`, and `expires_at` for a direct PUT with
    no store credentials on the worker. The shared filesystem carries
    `path`, the store-root relative path the worker must write, and no
    URL. When the latest stored bytes already match the presigned
    digest the reply carries `unchanged` instead: no URL, no path, and
    no `upload_id`; the worker skips the upload. Quota failures arrive
    as `ok: False` with a store code (`artifact_store`,
    `artifact_too_large`, `workspace_too_large`). `file_id` was set only
    for the kind `input_image`, which the API now refuses, so it is
    never set.
    """

    type: Literal["artifact.presign.reply"] = "artifact.presign.reply"
    session_id: uuid.UUID | None = None
    request_id: uuid.UUID
    ok: bool = True
    unchanged: bool = False
    upload_id: uuid.UUID | None = None
    artifact_id: uuid.UUID | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    expires_at: str | None = None
    path: str | None = None
    object_id: str | None = None
    file_id: str | None = None
    code: str | None = None
    message: str | None = None


class SearchRequest(ControlMessage):
    """Worker to API. One `web_search` call, a synchronous request.

    It is not an envelope: it never enters the outbox, has no `seq`, and
    is never acked or replayed. The worker waits for the matching
    `search.reply` with a timeout. A dropped socket fails the waiter and
    the model gets a tool error. It carries no provider name and no key.
    """

    type: Literal["search.request"] = "search.request"
    request_id: uuid.UUID
    session_id: uuid.UUID
    turn_id: uuid.UUID
    query: str
    max_results: int | None = Field(default=None, ge=1)


class SearchResultItem(ControlMessage):
    title: str
    url: str
    snippet: str = ""
    published_date: str | None = None


class SearchReply(ControlMessage):
    """API to worker. Answer to one `search.request`.

    `results` has the same shape for every provider. A failure arrives
    as `ok: False` with a short `code` (`search_denied`,
    `search_unavailable`, `search_timeout`, `search_failed`,
    `invalid_request`) and a `message` that is safe to show the model.
    """

    type: Literal["search.reply"] = "search.reply"
    session_id: uuid.UUID
    request_id: uuid.UUID
    ok: bool = True
    results: list[SearchResultItem] = Field(default_factory=list)
    code: str | None = None
    message: str | None = None
