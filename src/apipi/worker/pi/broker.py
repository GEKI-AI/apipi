import asyncio
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from apipi.config import Settings
from apipi.mcp.guard import McpConnectError, check_mcp_url, split_allow_hosts
from apipi.mcp.http import McpHttpServer
from apipi.protocol import SearchResultItem

log = logging.getLogger("apipi.worker.pi")

SearchHook = Callable[[str, str, str, int | None], Awaitable[dict[str, Any]]]

SEARCH_TIMEOUT = 35.0
SEARCH_MAX_QUERY = 2000
SNIPPET_LIMIT = 500
TITLE_LIMIT = 200
URL_LIMIT = 500
_SEARCH_ERRORS = {
    "search_denied": "Web search is not allowed for this session",
    "search_unavailable": "Web search is not available right now",
    "search_timeout": "Web search timed out",
    "search_failed": "Web search failed",
    "invalid_request": "Web search request was rejected",
}
_SPACE = re.compile(r"\s+")
DUMMY_KEY = "apipi"
_HOP = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


class SearchHookError(Exception):
    """The search hook cannot answer; the model gets a tool error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clip(value: str, limit: int) -> str:
    text = _SPACE.sub(" ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def format_search_results(results: list[SearchResultItem]) -> str:
    """Numbered text list for the model. Result text is untrusted."""
    if not results:
        return "No results."
    blocks: list[str] = []
    for index, item in enumerate(results, start=1):
        lines = [f"{index}. {_clip(item.title, TITLE_LIMIT) or item.url}"]
        lines.append(f"   URL: {_clip(item.url, URL_LIMIT)}")
        if item.published_date:
            lines.append(f"   Date: {_clip(item.published_date, 40)}")
        snippet = _clip(item.snippet, SNIPPET_LIMIT)
        if snippet:
            lines.append(f"   {snippet}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _join(base: str, path: str) -> str:
    root = base if base.endswith("/") else base + "/"
    return urljoin(root, path)


def _filter_headers(headers: Any) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP}


def strip_apipi_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if not key.lower().startswith("x-apipi-")
    }


@dataclass(frozen=True)
class McpRoute:
    route_id: str
    upstream: str
    headers: dict[str, str]


class _BrokerServer(uvicorn.Server):
    async def startup(self, sockets: list[Any] | None = None) -> None:
        try:
            await super().startup(sockets)
        except SystemExit as exc:
            raise OSError("credential broker bind failed") from exc


class SessionBroker:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        public_host: str,
        token: str,
        model_base_url: str,
        model_key: str | None,
        mcp_routes: list[McpRoute],
        attribution: bool = True,
        allow_hosts: tuple[str, ...] = (),
    ) -> None:
        self.host = host
        self.port = port
        self.public_host = public_host
        self.token = token
        self.model_base_url = model_base_url
        self.model_key = model_key
        self.mcp_routes = {route.route_id: route for route in mcp_routes}
        self.attribution = attribution
        self.allow_hosts = allow_hosts
        self._session_id: str | None = None
        self._agent_id: str | None = None
        self._turn_id: str | None = None
        self._search: SearchHook | None = None
        self.search_timeout = SEARCH_TIMEOUT
        self._client = httpx.AsyncClient(timeout=None)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._app = Starlette(
            routes=[
                Route(
                    "/{token}/v1",
                    self._model,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                ),
                Route(
                    "/{token}/v1/{path:path}",
                    self._model,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                ),
                Route("/{token}/search", self._search_route, methods=["POST"]),
                Route(
                    "/{token}/mcp/{route_id}",
                    self._mcp,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                ),
                Route(
                    "/{token}/mcp/{route_id}/{path:path}",
                    self._mcp,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                ),
            ]
        )

    @property
    def openai_base_url(self) -> str:
        return f"http://{self.public_host}:{self.port}/{self.token}/v1"

    def mcp_url(self, route_id: str) -> str:
        return f"http://{self.public_host}:{self.port}/{self.token}/mcp/{route_id}"

    @property
    def search_url(self) -> str:
        return f"http://{self.public_host}:{self.port}/{self.token}/search"

    def set_context(self, session_id: str | None, agent_id: str | None) -> None:
        self._session_id = session_id
        self._agent_id = agent_id

    def set_model_key(self, key: str | None) -> None:
        if key:
            self.model_key = key

    def set_turn(self, turn_id: str | None) -> None:
        self._turn_id = turn_id

    def clear_turn(self) -> None:
        self._turn_id = None
        self._search = None

    def set_search(self, hook: SearchHook | None) -> None:
        self._search = hook

    def attribution_headers(self) -> dict[str, str]:
        if not self.attribution:
            return {}
        headers: dict[str, str] = {}
        if self._session_id is not None:
            headers["x-apipi-session-id"] = self._session_id
        if self._turn_id is not None:
            headers["x-apipi-turn-id"] = self._turn_id
        if self._agent_id is not None:
            headers["x-apipi-agent-id"] = self._agent_id
        return headers

    def _check_token(self, request: Request) -> bool:
        got = str(request.path_params.get("token") or "")
        return secrets.compare_digest(got, self.token)

    async def _forward(
        self, request: Request, url: str, extra: dict[str, str]
    ) -> Response:
        headers = strip_apipi_headers(_filter_headers(request.headers))
        headers.update(extra)
        body = await request.body()
        upstream = self._client.build_request(
            request.method, url, headers=headers, content=body
        )
        response = await self._client.send(upstream, stream=True)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            stream(),
            status_code=response.status_code,
            headers=_filter_headers(response.headers),
        )

    async def _model(self, request: Request) -> Response:
        if not self._check_token(request):
            return Response(status_code=404)
        path = str(request.path_params.get("path") or "")
        url = _join(self.model_base_url, path)
        extra: dict[str, str] = {}
        if self.model_key:
            extra["Authorization"] = f"Bearer {self.model_key}"
        extra.update(self.attribution_headers())
        return await self._forward(request, url, extra)

    async def _mcp(self, request: Request) -> Response:
        if not self._check_token(request):
            return Response(status_code=404)
        route_id = str(request.path_params.get("route_id") or "")
        route = self.mcp_routes.get(route_id)
        if route is None:
            return Response(status_code=404)
        try:
            await check_mcp_url(
                route.upstream, label=route_id, allow_hosts=self.allow_hosts
            )
        except McpConnectError as exc:
            return Response(status_code=502, content=str(exc))
        path = str(request.path_params.get("path") or "")
        url = _join(route.upstream, path)
        return await self._forward(request, url, route.headers)

    async def _search_route(self, request: Request) -> Response:
        if not self._check_token(request):
            return Response(status_code=404)
        hook = self._search
        session_id = self._session_id
        turn_id = self._turn_id
        if hook is None or session_id is None or turn_id is None:
            return _search_error(409, "Web search is only available during a turn")
        try:
            body = await request.json()
        except ValueError:
            return _search_error(400, "Request body must be JSON")
        query = body.get("query") if isinstance(body, dict) else None
        if not isinstance(query, str) or not query.strip():
            return _search_error(400, "query is required")
        if len(query) > SEARCH_MAX_QUERY:
            return _search_error(400, "query is too long")
        max_results = body.get("max_results")
        if max_results is not None and (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or max_results < 1
        ):
            return _search_error(400, "max_results must be a positive integer")
        try:
            reply = await asyncio.wait_for(
                hook(session_id, turn_id, query, max_results),
                timeout=self.search_timeout,
            )
        except TimeoutError:
            return _search_error(200, _SEARCH_ERRORS["search_timeout"])
        except SearchHookError as exc:
            return _search_error(200, exc.message)
        except Exception:
            log.exception("search hook failed", extra={"session_id": session_id})
            return _search_error(200, _SEARCH_ERRORS["search_failed"])
        if not isinstance(reply, dict):
            return _search_error(200, _SEARCH_ERRORS["search_failed"])
        if reply.get("ok") is not True:
            code = reply.get("code")
            message = reply.get("message")
            if isinstance(message, str) and message.strip():
                return _search_error(200, _clip(message, 300))
            fallback = _SEARCH_ERRORS.get(str(code), _SEARCH_ERRORS["search_failed"])
            return _search_error(200, fallback)
        raw = reply.get("results")
        try:
            results = [SearchResultItem.model_validate(item) for item in raw or []]
        except ValueError:
            return _search_error(200, _SEARCH_ERRORS["search_failed"])
        return JSONResponse({"ok": True, "text": format_search_results(results)})

    async def start(self) -> None:
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="error",
            lifespan="off",
            access_log=False,
        )
        self._server = _BrokerServer(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            if self._task.done():
                exc = self._task.exception()
                if exc is not None:
                    raise exc
                raise RuntimeError("credential broker failed to start")
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("credential broker failed to start")
        if self.port == 0:
            sockets = []
            for server in self._server.servers:
                sockets.extend(server.sockets)
            if sockets:
                self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self._server = None
        await self._client.aclose()


def _search_error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


def _mcp_routes(mcp_http: list[McpHttpServer] | None) -> list[McpRoute]:
    routes: list[McpRoute] = []
    for index, server in enumerate(mcp_http or []):
        routes.append(
            McpRoute(
                route_id=str(index),
                upstream=server.server_url,
                headers=dict(server.headers),
            )
        )
    return routes


async def start_broker(
    settings: Settings,
    *,
    api_key: str | None,
    mcp_http: list[McpHttpServer] | None,
    host: str,
    port: int,
    public_host: str | None = None,
) -> SessionBroker:
    base = settings.model_base_url or "http://127.0.0.1"
    key = api_key if api_key else settings.model_api_key_overwrite
    allow_hosts = split_allow_hosts(settings.mcp_allow_hosts)
    for server in mcp_http or []:
        await check_mcp_url(
            server.server_url,
            label=server.server_label,
            allow_hosts=allow_hosts,
        )
    broker = SessionBroker(
        host=host,
        port=port,
        public_host=public_host if public_host is not None else host,
        token=secrets.token_urlsafe(16),
        model_base_url=base,
        model_key=key,
        mcp_routes=_mcp_routes(mcp_http),
        attribution=settings.model_attribution_headers,
        allow_hosts=allow_hosts,
    )
    await broker.start()
    return broker
