import asyncio
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer

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


def _join(base: str, path: str) -> str:
    root = base if base.endswith("/") else base + "/"
    return urljoin(root, path)


def _filter_headers(headers: Any) -> dict[str, str]:
    return {key: value for key, value in headers.items() if key.lower() not in _HOP}


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
    ) -> None:
        self.host = host
        self.port = port
        self.public_host = public_host
        self.token = token
        self.model_base_url = model_base_url
        self.model_key = model_key
        self.mcp_routes = {route.route_id: route for route in mcp_routes}
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

    def _check_token(self, request: Request) -> bool:
        got = str(request.path_params.get("token") or "")
        return secrets.compare_digest(got, self.token)

    async def _forward(
        self, request: Request, url: str, extra: dict[str, str]
    ) -> Response:
        headers = _filter_headers(request.headers)
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
        return await self._forward(request, url, extra)

    async def _mcp(self, request: Request) -> Response:
        if not self._check_token(request):
            return Response(status_code=404)
        route_id = str(request.path_params.get("route_id") or "")
        route = self.mcp_routes.get(route_id)
        if route is None:
            return Response(status_code=404)
        path = str(request.path_params.get("path") or "")
        url = _join(route.upstream, path)
        return await self._forward(request, url, route.headers)

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


def _blocked_host(url: str) -> bool:
    host = urlparse(url).hostname
    if host is None:
        return True
    return host in {"169.254.169.254", "metadata.google.internal"}


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
    for server in mcp_http or []:
        if _blocked_host(server.server_url):
            raise ValueError(f"blocked MCP host: {server.server_url}")
    broker = SessionBroker(
        host=host,
        port=port,
        public_host=public_host if public_host is not None else host,
        token=secrets.token_urlsafe(16),
        model_base_url=base,
        model_key=key,
        mcp_routes=_mcp_routes(mcp_http),
    )
    await broker.start()
    return broker
