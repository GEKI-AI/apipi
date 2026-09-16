import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.api.agents import AgentWrite
from apipi.api.deps import model_key
from apipi.auth import get_db, not_found, require_tenant
from apipi.env.hub import EnvironmentHub
from apipi.env.setup import SetupError, prepare_workspace
from apipi.env.spec import EnvironmentSpec, environment_payload
from apipi.errors import ApiError, gone
from apipi.execution import Execution
from apipi.mcp.http import (
    McpConnectError,
    apply_vault_headers,
    connect_mcp_http_tools,
)
from apipi.mcp.stdio import start_mcp_stdio_tools, stop_mcp_stdio
from apipi.otel import set_span, start_span
from apipi.pi.artifacts import wipe_artifact_store, wipe_workspace
from apipi.pi.dirs import session_workspace
from apipi.request_id import request_id_of
from apipi.runtime import (
    EventHub,
    event_body,
    fail_session,
    fail_stale_in_progress,
    persist_event,
)
from apipi.schemas import StrictModel
from apipi.skills import copy_capability_directories
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import Artifact, Item, SessionRow, Tenant, Turn
from apipi.store.repo import (
    create_environment,
    create_session,
    delete_session,
    delete_session_artifact,
    get_agent,
    get_session,
    get_session_artifact,
    get_session_turn,
    get_vault,
    list_artifacts,
    list_credentials_for_vault_ids,
    list_items,
    list_sessions,
    list_turns,
    update_session,
)
from apipi.tokens import hash_token
from apipi.usage import usage_from

router = APIRouter()


def _require_capacity(
    request: Request, session_id: uuid.UUID, tenant_id: uuid.UUID
) -> None:
    execution: Execution = request.app.state.execution
    code = execution.capacity_code(session_id, tenant_id)
    if code is None:
        return
    message = (
        "Too many live sessions for this tenant"
        if code == "capacity_tenant"
        else "Too many live sessions"
    )
    raise ApiError(
        "invalid_request",
        message,
        code=code,
        status_code=429,
    )


class SessionCreate(StrictModel):
    agent: AgentWrite | None = None
    agent_id: uuid.UUID | None = None
    environment: EnvironmentSpec | None = None
    input: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    stream: bool = False
    vault_ids: list[uuid.UUID] | None = None


class SessionUpdate(StrictModel):
    metadata: dict[str, Any] | None = None


class SessionInput(StrictModel):
    type: str
    content: str | None = None
    text: str | None = None
    turn_id: uuid.UUID | None = None
    call_id: str | None = None
    success: bool | None = None
    output: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def known_input(self) -> Self:
        if self.type == "agent.session.input.message":
            return self
        if self.type == "agent.session.input.cancel":
            return self
        if self.type == "agent.session.input.tool_result":
            if self.turn_id is None or self.call_id is None or self.success is None:
                raise ValueError("tool_result needs turn_id, call_id, and success")
            if self.success and self.output is None:
                raise ValueError("tool_result success needs output")
            if not self.success and self.error is None:
                raise ValueError("tool_result failure needs error")
            return self
        raise PydanticCustomError(
            "not_implemented",
            "{field} is not implemented",
            {"field": self.type},
        )


class OpenAIInputText(StrictModel):
    type: str
    text: str | None = None

    @model_validator(mode="after")
    def input_text_only(self) -> Self:
        if self.type != "input_text":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": self.type},
            )
        if self.text is None:
            raise ValueError("input_text needs text")
        return self


class OpenAIMessageInput(StrictModel):
    role: str
    content: list[OpenAIInputText]


class OpenAISessionEvent(StrictModel):
    type: str
    input: list[OpenAIMessageInput] | None = None
    turn_id: uuid.UUID | None = None
    call_id: str | None = None
    success: bool | None = None
    output: str | None = None
    error: str | None = None


class OpenAIEventsBody(StrictModel):
    events: list[OpenAISessionEvent]

    @model_validator(mode="after")
    def one_event(self) -> Self:
        if len(self.events) != 1:
            raise ValueError("events must contain exactly one event")
        self.to_session_input()
        return self

    def to_session_input(self) -> SessionInput:
        event = self.events[0]
        if event.type == "agent.session.input.message":
            return SessionInput(type=event.type, content=_first_input_text(event.input))
        return SessionInput(
            type=event.type,
            turn_id=event.turn_id,
            call_id=event.call_id,
            success=event.success,
            output=event.output,
            error=event.error,
        )


def _first_input_text(messages: list[OpenAIMessageInput] | None) -> str:
    if not messages:
        raise ValueError("message needs input")
    for message in messages:
        for part in message.content:
            if part.text is not None:
                return part.text
    raise ValueError("message needs input_text")


SessionEventBody = OpenAIEventsBody | SessionInput


def turn_body(turn: Turn) -> dict[str, Any]:
    return {
        "id": str(turn.id),
        "session_id": str(turn.session_id),
        "status": turn.status,
        "usage": usage_from(turn.usage) if turn.usage is not None else None,
        "created_at": turn.created_at.isoformat(),
        "updated_at": turn.updated_at.isoformat(),
    }


def item_body(item: Item) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "session_id": str(item.session_id),
        "turn_id": str(item.turn_id) if item.turn_id is not None else None,
        "type": item.type,
        "data": item.data,
        "created_at": item.created_at.isoformat(),
    }


def artifact_body(artifact: Artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "session_id": str(artifact.session_id),
        "turn_id": str(artifact.turn_id) if artifact.turn_id is not None else None,
        "path": artifact.path,
        "content_type": artifact.content_type,
        "created_at": artifact.created_at.isoformat(),
    }


def session_body(row: SessionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id is not None else None,
        "status": row.status,
        "environment": row.environment,
        "metadata": row.metadata_json,
        "required_actions": row.required_actions,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "vault_ids": [str(item) for item in (row.vault_ids or [])],
    }


def _key_id(request: Request) -> str | None:
    value = getattr(request.state, "key_id", None)
    return value if isinstance(value, str) else None


def _input_text(value: str | dict[str, Any] | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    content = value.get("content")
    if isinstance(content, str):
        return content
    text = value.get("text")
    if isinstance(text, str):
        return text
    return ""


def _sse(event: dict[str, Any]) -> str:
    prefix = f"id: {event['seq']}\n" if "seq" in event else ""
    return f"{prefix}event: {event['type']}\ndata: {json.dumps(event)}\n\n"


SSE_PING = ": ping\n"


async def _event_stream(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None,
) -> AsyncGenerator[str]:
    queue = hub.subscribe(session_id)
    try:
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id, after_seq=after_seq)
        last = after_seq or 0
        ping_at = 0.0
        for event in existing:
            last = event.seq
            yield _sse(event_body(event))
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                async with store.session() as db:
                    extra = await list_events(db, tenant_id, session_id, after_seq=last)
                if extra:
                    for event in extra:
                        last = event.seq
                        yield _sse(event_body(event))
                    continue
                ping_at += 0.25
                if ping_at >= 15:
                    ping_at = 0.0
                    yield SSE_PING
                continue
            ping_at = 0.0
            seq = payload.get("seq")
            if seq is None:
                yield _sse(payload)
                continue
            seq = int(seq)
            if seq <= last:
                continue
            last = seq
            yield _sse(payload)
    finally:
        hub.unsubscribe(session_id, queue)


@router.post("/v1/agents/sessions")
async def create_agent_session(
    body: SessionCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    if (body.agent is None) == (body.agent_id is None):
        raise ApiError(
            "invalid_request",
            "Provide agent or agent_id",
            code="invalid_request",
        )
    agent_id = body.agent_id
    store: Store = request.app.state.store
    hub: EventHub = request.app.state.event_hub
    execution: Execution = request.app.state.execution
    environment = environment_payload(body.environment)
    raw_tools: list[Any] = []
    env_key: str | None = None
    env_id: uuid.UUID | None = None
    model: str | None = None
    instructions: str | None = None
    async with store.session() as db:
        if agent_id is not None:
            agent = await get_agent(db, tenant.id, agent_id)
            if agent is None:
                not_found()
            raw_tools = agent.tools
            model = agent.model
        elif body.agent is not None:
            model = body.agent.model
            instructions = body.agent.instructions
            if body.agent.tools is not None:
                raw_tools = [
                    tool.model_dump(exclude_none=True) for tool in body.agent.tools
                ]
        key_id = getattr(request.state, "key_id", "")
        vault_ids = [str(item) for item in (body.vault_ids or [])]
        for vault_id in body.vault_ids or []:
            if await get_vault(db, tenant.id, vault_id) is None:
                not_found()
        row = await create_session(
            db,
            tenant.id,
            agent_id=agent_id,
            model=model if agent_id is None else None,
            instructions=instructions if agent_id is None else None,
            environment=environment,
            metadata=body.metadata,
            key_id=key_id if isinstance(key_id, str) else "",
            vault_ids=vault_ids,
        )
        if environment.get("type") == "openai_hosted":
            directory = session_workspace(request.app.state.settings, tenant.id, row.id)
            caps = environment.get("capability_directories")
            if isinstance(caps, list):
                copy_capability_directories(
                    directory, [item for item in caps if isinstance(item, str)]
                )
            try:
                prepare_workspace(directory, environment)
            except SetupError as exc:
                raise ApiError(
                    "invalid_request",
                    exc.message,
                    code="invalid_request",
                ) from exc
            environment = {**environment, "directory": str(directory)}
            row.environment = environment
            await db.flush()
        elif environment.get("type") == "self_hosted":
            env_id = uuid.uuid4()
            env_key = secrets.token_urlsafe(32)
            environment = {**environment, "id": str(env_id)}
            row.environment = environment
            row.required_actions = [
                {
                    "type": "environment_connection",
                    "environment_id": str(env_id),
                }
            ]
            await create_environment(
                db,
                tenant.id,
                row.id,
                environment_id=env_id,
                key_hash=hash_token(env_key),
            )
            await db.flush()
        await persist_event(
            db,
            hub,
            tenant.id,
            row.id,
            type="agent.session.created",
            data={"id": str(row.id)},
        )
        if env_id is not None:
            await persist_event(
                db,
                hub,
                tenant.id,
                row.id,
                type="agent.session.environment.pending",
                data={"environment_id": str(env_id)},
            )
        session_id = row.id
    tracing = request.app.state.tracing
    request_id = request_id_of(request)
    with start_span(
        tracing,
        "session",
        request_id=request_id,
        session_id=session_id,
        model=model,
    ):
        try:
            connected = await connect_mcp_http_tools(raw_tools)
            if vault_ids:
                async with store.session() as db:
                    creds = await list_credentials_for_vault_ids(
                        db,
                        tenant.id,
                        [uuid.UUID(item) for item in vault_ids],
                    )
                connected = apply_vault_headers(connected, creds)
            stdio = await start_mcp_stdio_tools(
                raw_tools,
                on_host=execution.stdio_on_host,
            )
        except McpConnectError as exc:
            async with store.session() as db:
                await fail_session(db, hub, tenant.id, session_id, str(exc))
                row = await get_session(db, tenant.id, session_id)
                if row is None:
                    not_found()
                set_span(tracing, status="failed")
                return session_body(row)
        request.app.state.mcp_http[session_id] = connected
        request.app.state.mcp_stdio[session_id] = stdio
        execution.put_stdio(session_id, stdio)
        text = _input_text(body.input)
        if text:
            _require_capacity(request, session_id, tenant.id)
            await execution.run_turn(
                tenant.id,
                session_id,
                text,
                mcp_http=connected,
                mcp_stdio=stdio,
                request_id=request_id,
                api_key=model_key(request),
                key_id=_key_id(request),
            )
        else:
            async with store.session() as db:
                await persist_event(
                    db, hub, tenant.id, session_id, type="agent.session.idle"
                )
    async with store.session() as db:
        row = await get_session(db, tenant.id, session_id)
        if row is None:
            not_found()
        payload = session_body(row)
        if env_id is not None and env_key is not None:
            payload["environment_id"] = str(env_id)
            payload["key"] = env_key
    if body.stream:
        return StreamingResponse(
            _event_stream(store, hub, tenant.id, session_id, None),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    return payload


@router.get("/v1/agents/sessions")
async def list_agent_sessions(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    hub: EventHub = request.app.state.event_hub
    rows = await list_sessions(db, tenant.id)
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.status == "in_progress" and hub.turn_abort(row.id) is None:
            recovered = await fail_stale_in_progress(db, hub, tenant.id, row.id)
            row = recovered if recovered is not None else row
        out.append(session_body(row))
    return {"data": out}


@router.get("/v1/agents/sessions/{session_id}")
async def read_agent_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
    hub: EventHub = request.app.state.event_hub
    if row.status == "in_progress" and hub.turn_abort(session_id) is None:
        recovered = await fail_stale_in_progress(db, hub, tenant.id, session_id)
        if recovered is not None:
            row = recovered
    return session_body(row)


@router.post("/v1/agents/sessions/{session_id}")
async def update_agent_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if body.metadata is not None:
        changes["metadata"] = body.metadata
    row = await update_session(db, tenant.id, session_id, changes=changes)
    if row is None:
        not_found()
    return session_body(row)


@router.delete("/v1/agents/sessions/{session_id}")
async def delete_agent_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
    env_id_raw = row.environment.get("id")
    directory = row.environment.get("directory")
    deleted = await delete_session(db, tenant.id, session_id)
    if not deleted:
        not_found()
    if isinstance(env_id_raw, str):
        env_hub: EnvironmentHub = request.app.state.env_hub
        await env_hub.close(uuid.UUID(env_id_raw))
    execution: Execution = request.app.state.execution
    await execution.teardown(session_id)
    if isinstance(directory, str) and directory:
        wipe_workspace(Path(directory))
    await wipe_artifact_store(
        request.app.state.blobs, tenant.id, row.key_id, session_id
    )
    request.app.state.mcp_http.pop(session_id, None)
    stdio = request.app.state.mcp_stdio.pop(session_id, None)
    if stdio:
        await stop_mcp_stdio(stdio)
    return {"id": str(session_id), "deleted": True}


@router.post("/v1/agents/sessions/{session_id}/events")
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionEventBody,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    store: Store = request.app.state.store
    execution: Execution = request.app.state.execution
    parsed = body.to_session_input() if isinstance(body, OpenAIEventsBody) else body
    text = parsed.content if parsed.content is not None else parsed.text
    if text is None:
        text = ""
    action = "message"
    stale = False
    cancel_status = ""
    async with store.session() as db:
        row = await get_session(db, tenant.id, session_id)
        if row is None:
            not_found()
        if parsed.type == "agent.session.input.cancel":
            action = "cancel"
            cancel_status = row.status
        elif parsed.type == "agent.session.input.tool_result":
            if (
                parsed.turn_id is None
                or parsed.call_id is None
                or parsed.success is None
            ):
                raise ApiError(
                    "invalid_request",
                    "tool_result needs turn_id, call_id, and success",
                    code="invalid_request",
                )
            action = "tool"
        else:
            if row.status == "requires_action":
                raise ApiError(
                    "invalid_request",
                    "Session is waiting for a tool result",
                    code="invalid_request",
                )
            action = "message"
            stale = row.status == "in_progress"
    tracing = request.app.state.tracing
    request_id = request_id_of(request)
    if action == "cancel":
        await execution.cancel(session_id, status=cancel_status)
    elif action == "tool":
        if parsed.turn_id is None or parsed.call_id is None or parsed.success is None:
            raise ApiError(
                "invalid_request",
                "tool_result needs turn_id, call_id, and success",
                code="invalid_request",
            )
        with start_span(
            tracing,
            "session",
            request_id=request_id,
            session_id=session_id,
        ):
            await execution.continue_turn(
                tenant.id,
                session_id,
                turn_id=parsed.turn_id,
                call_id=parsed.call_id,
                success=parsed.success,
                output=parsed.output,
                error=parsed.error,
                mcp_http=request.app.state.mcp_http.get(session_id),
                mcp_stdio=request.app.state.mcp_stdio.get(session_id),
                request_id=request_id,
                api_key=model_key(request),
                key_id=_key_id(request),
            )
    else:
        if stale:
            await execution.prepare_for_new_turn(tenant.id, session_id)
        with start_span(
            tracing,
            "session",
            request_id=request_id,
            session_id=session_id,
        ):
            _require_capacity(request, session_id, tenant.id)
            await execution.run_turn(
                tenant.id,
                session_id,
                text,
                mcp_http=request.app.state.mcp_http.get(session_id),
                mcp_stdio=request.app.state.mcp_stdio.get(session_id),
                request_id=request_id,
                api_key=model_key(request),
                key_id=_key_id(request),
            )
    async with store.session() as db:
        row = await get_session(db, tenant.id, session_id)
        if row is None:
            not_found()
        return session_body(row)


@router.get("/v1/agents/sessions/{session_id}/events")
async def get_session_events(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    stream: bool = False,
    after_seq: int | None = Query(default=None),
) -> Any:
    store: Store = request.app.state.store
    hub: EventHub = request.app.state.event_hub
    async with store.session() as db:
        row = await get_session(db, tenant.id, session_id)
        if row is None:
            not_found()
        if not stream:
            events = await list_events(db, tenant.id, session_id, after_seq=after_seq)
            return {"data": [event_body(event) for event in events]}
    return StreamingResponse(
        _event_stream(store, hub, tenant.id, session_id, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/v1/agents/sessions/{session_id}/export")
async def export_agent_session(
    session_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
    events = await list_events(db, tenant.id, session_id)
    turns = await list_turns(db, tenant.id, session_id)
    items = await list_items(db, tenant.id, session_id)
    if turns is None or items is None:
        not_found()
    return {
        "events": [event_body(event) for event in events],
        "turns": [turn_body(turn) for turn in turns],
        "items": [item_body(item) for item in items],
    }


@router.get("/v1/agents/sessions/{session_id}/turns")
async def list_session_turns(
    session_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    turns = await list_turns(db, tenant.id, session_id)
    if turns is None:
        not_found()
    return {"data": [turn_body(turn) for turn in turns]}


@router.get("/v1/agents/sessions/{session_id}/turns/{turn_id}")
async def read_session_turn(
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    turn = await get_session_turn(db, tenant.id, session_id, turn_id)
    if turn is None:
        not_found()
    return turn_body(turn)


@router.get("/v1/agents/sessions/{session_id}/items")
async def list_session_items(
    session_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    items = await list_items(db, tenant.id, session_id)
    if items is None:
        not_found()
    return {"data": [item_body(item) for item in items]}


@router.get("/v1/agents/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    artifacts = await list_artifacts(db, tenant.id, session_id)
    if artifacts is None:
        not_found()
    return {"data": [artifact_body(artifact) for artifact in artifacts]}


@router.get("/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content")
async def read_session_artifact_content(
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Any:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
    artifact = await get_session_artifact(db, tenant.id, session_id, artifact_id)
    if artifact is None:
        not_found()
    data = await request.app.state.blobs.get(
        tenant.id, artifact.key_id, session_id, artifact_id
    )
    if data is None:
        gone()
    return Response(
        content=data,
        media_type=artifact.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{Path(artifact.path).name}"'
        },
    )


@router.delete("/v1/agents/sessions/{session_id}/artifacts/{artifact_id}")
async def delete_agent_session_artifact(
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
    artifact = await get_session_artifact(db, tenant.id, session_id, artifact_id)
    if artifact is None:
        not_found()
    await request.app.state.blobs.delete(
        tenant.id, artifact.key_id, session_id, artifact_id
    )
    deleted = await delete_session_artifact(db, tenant.id, session_id, artifact_id)
    if deleted is None:
        not_found()
    return {"id": str(artifact_id), "deleted": True}
