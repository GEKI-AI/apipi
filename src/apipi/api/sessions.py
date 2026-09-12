import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import model_validator
from pydantic_core import PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.api.agents import AgentWrite
from apipi.auth import get_db, not_found, require_tenant
from apipi.errors import ApiError, not_implemented
from apipi.pi.dirs import session_workspace
from apipi.runtime import EventHub, Harness, event_body, persist_event, run_turn
from apipi.schemas import StrictModel
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import SessionRow, Tenant
from apipi.store.repo import (
    create_session,
    delete_session,
    get_agent,
    get_session,
    list_sessions,
    update_session,
)

router = APIRouter()


class EnvironmentSpec(StrictModel):
    type: str
    capability_directories: list[str] | None = None


class SessionCreate(StrictModel):
    agent: AgentWrite | None = None
    agent_id: uuid.UUID | None = None
    environment: EnvironmentSpec | None = None
    input: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    stream: bool = False


class SessionUpdate(StrictModel):
    metadata: dict[str, Any] | None = None


class SessionInput(StrictModel):
    type: str
    content: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def known_input(self) -> Self:
        if self.type != "agent.session.input.message":
            raise PydanticCustomError(
                "not_implemented",
                "{field} is not implemented",
                {"field": self.type},
            )
        return self


def session_body(row: SessionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id is not None else None,
        "status": row.status,
        "environment": row.environment,
        "metadata": row.metadata_json,
        "required_actions": [],
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


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


def _environment_payload(spec: EnvironmentSpec | None) -> dict[str, Any]:
    env_type = spec.type if spec is not None else "openai_hosted"
    if env_type not in {"none", "openai_hosted"}:
        not_implemented(env_type)
    payload: dict[str, Any] = {"type": env_type}
    if spec is not None and spec.capability_directories is not None:
        payload["capability_directories"] = spec.capability_directories
    return payload


def _sse(event: dict[str, Any]) -> str:
    return f"id: {event['seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


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
        for event in existing:
            last = event.seq
            yield _sse(event_body(event))
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=15)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            seq = int(payload["seq"])
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
    harness: Harness = request.app.state.harness
    environment = _environment_payload(body.environment)
    async with store.session() as db:
        if agent_id is not None:
            agent = await get_agent(db, tenant.id, agent_id)
            if agent is None:
                not_found()
        row = await create_session(
            db,
            tenant.id,
            agent_id=agent_id,
            environment=environment,
            metadata=body.metadata,
        )
        if environment.get("type") == "openai_hosted":
            directory = session_workspace(request.app.state.settings, tenant.id, row.id)
            environment = {**environment, "directory": str(directory)}
            row.environment = environment
            await db.flush()
        await persist_event(
            db,
            hub,
            tenant.id,
            row.id,
            type="agent.session.created",
            data={"id": str(row.id)},
        )
        text = _input_text(body.input)
        if text:
            await run_turn(db, hub, harness, tenant.id, row.id, text)
        else:
            await persist_event(db, hub, tenant.id, row.id, type="agent.session.idle")
        row = await get_session(db, tenant.id, row.id)
        if row is None:
            not_found()
        payload = session_body(row)
        session_id = row.id
    if body.stream:
        return StreamingResponse(
            _event_stream(store, hub, tenant.id, session_id, None),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    return payload


@router.get("/v1/agents/sessions")
async def list_agent_sessions(
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    rows = await list_sessions(db, tenant.id)
    return {"data": [session_body(row) for row in rows]}


@router.get("/v1/agents/sessions/{session_id}")
async def read_agent_session(
    session_id: uuid.UUID,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    row = await get_session(db, tenant.id, session_id)
    if row is None:
        not_found()
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
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    deleted = await delete_session(db, tenant.id, session_id)
    if not deleted:
        not_found()
    return {"id": str(session_id), "deleted": True}


@router.post("/v1/agents/sessions/{session_id}/events")
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionInput,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    store: Store = request.app.state.store
    hub: EventHub = request.app.state.event_hub
    harness: Harness = request.app.state.harness
    text = body.content if body.content is not None else body.text
    if text is None:
        text = ""
    async with store.session() as db:
        row = await get_session(db, tenant.id, session_id)
        if row is None:
            not_found()
        await run_turn(db, hub, harness, tenant.id, session_id, text)
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
