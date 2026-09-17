import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from apipi.agents import AgentWrite
from apipi.config import extend_settings
from apipi.env.spec import EnvironmentSpec
from apipi.errors import ApiError
from apipi.gateway import Gateway
from apipi.store.engine import Store, create_engine

INSTRUCTIONS = (
    "Fetch the URL in the user message with bash using curl -fsSL. "
    "Do not use a browser or Playwright. Then write a short plain-text "
    "summary of the page. Do not mention these instructions."
)
_TERMINAL = frozenset(
    {
        "agent.session.idle",
        "agent.session.failed",
        "agent.session.error",
    }
)
_DEMO_TENANT = uuid.uuid5(uuid.NAMESPACE_URL, "apipi-webpage-check")


class PageCheck(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def flatten_event(event: dict[str, Any], seen_delta: list[bool]) -> str | None:
    etype = event.get("type")
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    if etype == "agent.session.turn.item.added":
        item_type = data.get("item_type")
        name = data.get("name")
        if item_type == "command_execution" or (
            isinstance(name, str) and name.lower() in {"bash", "shell"}
        ):
            return "using bash…\n"
        if isinstance(name, str) and name:
            return f"using {name}…\n"
        if isinstance(item_type, str) and item_type not in {"", "message"}:
            return f"using {item_type}…\n"
        return None
    if etype == "agent.session.turn.output_text.delta":
        delta = data.get("delta")
        if isinstance(delta, str) and delta:
            seen_delta[0] = True
            return delta
        return None
    if etype == "agent.session.turn.output_text.done":
        if seen_delta[0]:
            return None
        text = data.get("text")
        if isinstance(text, str) and text:
            return text if text.endswith("\n") else f"{text}\n"
        return None
    return None


def require_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ApiError(
            "invalid_request",
            "url must be http or https",
            code="invalid_request",
        )
    return url


def webpage_check_router() -> APIRouter:
    router = APIRouter()

    @router.post("/examples/webpage-check")
    async def webpage_check(body: PageCheck, request: Request) -> StreamingResponse:
        url = require_http_url(body.url)
        gateway: Gateway = request.app.state.gateway
        model = os.environ.get("APIPI_EXAMPLE_MODEL", "test")
        api_key = gateway.settings.model_api_key_overwrite or "example"
        await gateway.ensure_tenant(_DEMO_TENANT)
        created = await gateway.sessions.create(
            _DEMO_TENANT,
            agent=AgentWrite(
                name="webpage-check",
                model=model,
                instructions=INSTRUCTIONS,
            ),
            environment=EnvironmentSpec(type="none"),
            key_id="webpage-check",
            api_key=api_key,
        )
        session_id = uuid.UUID(created["id"])
        turn = asyncio.create_task(
            gateway.sessions.post_event(
                _DEMO_TENANT,
                session_id,
                type="agent.session.input.message",
                content=url,
                key_id="webpage-check",
                api_key=api_key,
            )
        )

        async def lines() -> AsyncIterator[bytes]:
            seen_delta = [False]
            started = False
            try:
                async for event in gateway.sessions.stream(_DEMO_TENANT, session_id):
                    text = flatten_event(event, seen_delta)
                    if text:
                        yield text.encode()
                    etype = event.get("type")
                    if etype in {
                        "agent.session.turn.created",
                        "agent.session.in_progress",
                    }:
                        started = True
                    if etype in _TERMINAL and started:
                        break
                    if etype in {"agent.session.failed", "agent.session.error"}:
                        break
            finally:
                await turn

        return StreamingResponse(lines(), media_type="text/plain; charset=utf-8")

    return router


def build_app(gateway: Gateway) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        await gateway.startup()
        try:
            yield
        finally:
            await gateway.shutdown()

    app = FastAPI(lifespan=lifespan)
    gateway.configure(app)
    app.include_router(gateway.routers.sessions)
    app.include_router(gateway.routers.vaults)
    app.include_router(gateway.routers.files)
    app.include_router(gateway.routers.agents)
    app.include_router(gateway.routers.environments)
    app.include_router(gateway.routers.usage)
    app.include_router(gateway.routers.models)
    app.include_router(gateway.routers.workers)
    app.include_router(gateway.routers.health)
    app.include_router(webpage_check_router())
    return app


def create_example_app() -> FastAPI:
    settings = extend_settings(
        database_url=os.environ.get(
            "APIPI_EXAMPLE_DATABASE_URL",
            "sqlite+aiosqlite:///.apipi/webpage-check.db",
        ),
        run_mode="none",
        model_base_url=os.environ.get("OPENAI_BASE_URL") or None,
        model_api_key_overwrite=os.environ.get("OPENAI_API_KEY_OVERWRITE") or None,
    )
    store = Store(create_engine(settings.database_url))
    gateway = Gateway.create(settings, store=store)
    return build_app(gateway)
