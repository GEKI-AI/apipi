from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from apipi.gateway.errors import error_body
from apipi.gateway.http_path import request_path, skip_request_path
from apipi.gateway.otel import current_trace_id

_SKIP_CONTEXT = frozenset({"/health", "/metrics"})
_CONTEXT_HEADERS = frozenset(
    {b"x-apipi-instance", b"x-tenant-id", b"x-user-id", b"x-trace-id"}
)


def _ascii_header(value: object) -> bytes | None:
    if value is None:
        return None
    text = str(value).strip()
    if (
        not text
        or len(text) > 512
        or not text.isascii()
        or "\r" in text
        or "\n" in text
    ):
        return None
    return text.encode("ascii")


def _trace_id_from_parent(value: str | None) -> str | None:
    if value is None:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4:
        return None
    trace_id = parts[1].lower()
    if len(trace_id) != 32 or trace_id == "0" * 32:
        return None
    try:
        int(trace_id, 16)
    except ValueError:
        return None
    return trace_id


def _header_value(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return value.decode("ascii")
            except UnicodeDecodeError:
                return None
    return None


class InstanceMiddleware:
    def __init__(self, app: ASGIApp, instance_id: str | None) -> None:
        self.app = app
        self.instance_id = instance_id.encode("ascii") if instance_id else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or skip_request_path(scope, _SKIP_CONTEXT):
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        incoming = _trace_id_from_parent(_header_value(scope, b"traceparent"))
        if incoming is not None:
            state["trace_id"] = incoming

        async def send_with_context(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name not in _CONTEXT_HEADERS
                ]
                extra: list[tuple[bytes, bytes]] = []
                if self.instance_id is not None:
                    extra.append((b"x-apipi-instance", self.instance_id))
                tenant = _ascii_header(state.get("tenant_id"))
                if tenant is not None:
                    extra.append((b"x-tenant-id", tenant))
                user = _ascii_header(state.get("key_id"))
                if user is not None:
                    extra.append((b"x-user-id", user))
                trace = current_trace_id() or state.get("trace_id")
                encoded = _ascii_header(trace) if trace is not None else None
                if encoded is not None:
                    extra.append((b"x-trace-id", encoded))
                headers.extend(extra)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_context)


class MaxBodyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int,
        *,
        file_max_bytes: int | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.file_max_bytes = (
            file_max_bytes if file_max_bytes is not None else max_bytes
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = request_path(scope)
            method = scope.get("method", "")
            limit = (
                self.file_max_bytes
                if method == "POST" and path in {"/v1/files", "/v1/skills"}
                else self.max_bytes
            )
            for key, value in scope.get("headers", []):
                if key == b"content-length":
                    try:
                        length = int(value)
                    except ValueError:
                        length = 0
                    if length > limit:
                        response = JSONResponse(
                            status_code=413,
                            content=error_body(
                                "invalid_request",
                                "Request body too large",
                                "payload_too_large",
                            ),
                        )
                        await response(scope, receive, send)
                        return
                    break
        await self.app(scope, receive, send)
