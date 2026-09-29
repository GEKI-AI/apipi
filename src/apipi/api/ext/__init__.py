import logging
import re
from collections.abc import Iterable
from typing import Any

from fastapi import APIRouter
from fastapi.routing import APIRoute

log = logging.getLogger("apipi.api")

_LOGGED: set[str] = set()

_REPLACEMENTS = (
    ("/v1/usage", "/v1/apipi/usage"),
    ("/v1/templates", "/v1/apipi/templates"),
    ("/v1/uploads", "/v1/apipi/uploads"),
    ("/v1/chat/", "/v1/apipi/chat/"),
    ("/v1/agents/{agent_id}/export", "/v1/apipi/agents/{agent_id}/export"),
    (
        "/v1/agents/sessions/{session_id}/export",
        "/v1/apipi/sessions/{session_id}/export",
    ),
    (
        "/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/download",
        "/v1/apipi/sessions/{session_id}/artifacts/{artifact_id}/download",
    ),
)

_ALIAS = re.compile(
    r"^/v1/usage(?:/|$)"
    r"|^/v1/templates(?:/|$)"
    r"|^/v1/uploads(?:/|$)"
    r"|^/v1/chat/"
    r"|^/v1/agents/[^/]+/export$"
    r"|^/v1/agents/sessions/[^/]+/export$"
    r"|^/v1/agents/sessions/[^/]+/artifacts/[^/]+/download$"
)


class AliasLogMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            path = scope.get("path")
            if isinstance(path, str):
                note_alias(path)
        await self.app(scope, receive, send)


def note_alias(path: str) -> None:
    if not _ALIAS.match(path):
        return
    key = path
    if path.startswith("/v1/usage"):
        key = "/v1/usage"
    elif path.startswith("/v1/templates"):
        key = "/v1/templates"
    elif path.startswith("/v1/uploads"):
        key = "/v1/uploads"
    elif path.startswith("/v1/chat/"):
        key = "/v1/chat/"
    elif path.endswith("/export") and "/sessions/" in path:
        key = "/v1/agents/sessions/{session_id}/export"
    elif path.endswith("/export"):
        key = "/v1/agents/{agent_id}/export"
    elif path.endswith("/download"):
        key = "/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/download"
    if key in _LOGGED:
        return
    _LOGGED.add(key)
    log.warning("deprecated route %s; use /v1/apipi", key)


def _canonical(path: str) -> str | None:
    for old, new in _REPLACEMENTS:
        if old.endswith("/"):
            if path.startswith(old):
                return new + path[len(old) :]
            continue
        if path == old or path.startswith(old + "/"):
            return new + path[len(old) :]
    return None


def include_ext(app: Any, routers: Iterable[APIRouter]) -> None:
    app.include_router(ext_router(routers))
    app.add_middleware(AliasLogMiddleware)


def ext_router(routers: Iterable[APIRouter]) -> APIRouter:
    ext = APIRouter()
    seen: set[tuple[str, str]] = set()
    for router in routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            target = _canonical(route.path)
            if target is None:
                continue
            route.deprecated = True
            methods = sorted(route.methods or [])
            for method in methods:
                key = (method, target)
                if key in seen:
                    continue
                seen.add(key)
                ext.add_api_route(
                    target,
                    route.endpoint,
                    methods=[method],
                    name=f"apipi_{route.name}_{method.lower()}",
                    response_model=route.response_model,
                    status_code=route.status_code,
                    tags=list(route.tags or []),
                    summary=route.summary,
                    description=route.description,
                    response_class=route.response_class,
                    deprecated=False,
                )
    return ext
