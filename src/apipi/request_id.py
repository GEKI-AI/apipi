import uuid

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_HEADER = b"x-request-id"
_CLIENT_HEADER = b"x-client-request-id"
_MAX_LEN = 512
_HEALTH = "/health"


def _decode(value: bytes) -> str | None:
    try:
        return value.decode("ascii")
    except UnicodeDecodeError:
        return None


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return _decode(value)
    return None


def valid_request_id(value: str) -> bool:
    return bool(value) and len(value) <= _MAX_LEN and value.isascii()


def resolve_request_id(client_id: str | None, incoming: str | None) -> str:
    if client_id is not None and valid_request_id(client_id):
        return client_id
    if incoming is not None and valid_request_id(incoming):
        return incoming
    return str(uuid.uuid4())


def request_id_of(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == _HEALTH:
            await self.app(scope, receive, send)
            return
        request_id = resolve_request_id(
            _header(scope, _CLIENT_HEADER),
            _header(scope, _HEADER),
        )
        scope.setdefault("state", {})["request_id"] = request_id
        encoded = request_id.encode("ascii")

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name != _HEADER
                ]
                headers.append((_HEADER, encoded))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_id)
