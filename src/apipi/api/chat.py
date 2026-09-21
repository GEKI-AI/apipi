import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from apipi.api.deps import model_key
from apipi.api.sessions import (
    OpenAIEventsBody,
    SessionEventBody,
    SessionUpdate,
    _key_id,
    _sse_response,
    _user_id,
)
from apipi.env.spec import EnvironmentSpec
from apipi.gateway.auth import not_found, require_tenant
from apipi.gateway.request_id import request_id_of
from apipi.gateway.schemas import StrictModel
from apipi.services.agents import AgentWrite
from apipi.services.sessions import (
    SessionService,
    chat_metadata,
    chat_session_body,
    is_chat_session,
)
from apipi.store.models import Tenant

router = APIRouter()


class ChatSessionCreate(StrictModel):
    agent: AgentWrite | None = None
    agent_id: uuid.UUID | None = None
    input: str | dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    stream: bool = False
    vault_ids: list[uuid.UUID] | None = None


def _sessions(request: Request) -> SessionService:
    return request.app.state.gateway.sessions


def _public(body: dict[str, Any]) -> dict[str, Any]:
    if not is_chat_session(body):
        not_found()
    return chat_session_body(body)


@router.post("/v1/chat/sessions")
async def create_chat_session(
    body: ChatSessionCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    sessions = _sessions(request)
    payload = await sessions.create(
        tenant.id,
        agent=body.agent,
        agent_id=body.agent_id,
        environment=EnvironmentSpec(type="none"),
        input=body.input,
        metadata=chat_metadata(body.metadata),
        vault_ids=body.vault_ids,
        key_id=_key_id(request),
        user_id=_user_id(request),
        request_id=request_id_of(request),
        api_key=model_key(request),
        wait_turn=not body.stream,
    )
    if body.stream:
        return _sse_response(
            sessions.store,
            sessions.event_hub,
            tenant.id,
            uuid.UUID(payload["id"]),
            None,
        )
    return chat_session_body(payload)


@router.get("/v1/chat/sessions")
async def list_chat_sessions(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    listed = await _sessions(request).list(tenant.id)
    return {
        "data": [
            chat_session_body(row) for row in listed["data"] if is_chat_session(row)
        ]
    }


@router.get("/v1/chat/sessions/{session_id}")
async def read_chat_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    return _public(await _sessions(request).get(tenant.id, session_id))


@router.post("/v1/chat/sessions/{session_id}")
async def update_chat_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    metadata = chat_metadata(body.metadata) if body.metadata is not None else None
    return chat_session_body(
        await sessions.update(tenant.id, session_id, metadata=metadata)
    )


@router.delete("/v1/chat/sessions/{session_id}")
async def delete_chat_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    return await sessions.delete(tenant.id, session_id)


@router.post("/v1/chat/sessions/{session_id}/events")
async def post_chat_session_event(
    session_id: uuid.UUID,
    body: SessionEventBody,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    parsed = body.to_session_input() if isinstance(body, OpenAIEventsBody) else body
    return await sessions.post_event(
        tenant.id,
        session_id,
        type=parsed.type,
        content=parsed.content,
        text=parsed.text,
        turn_id=parsed.turn_id,
        call_id=parsed.call_id,
        success=parsed.success,
        output=parsed.output,
        error=parsed.error,
        key_id=_key_id(request) or None,
        user_id=_user_id(request),
        request_id=request_id_of(request),
        api_key=model_key(request),
    )


@router.get("/v1/chat/sessions/{session_id}/events")
async def get_chat_session_events(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    stream: bool = False,
    after_seq: int | None = Query(default=None),
) -> Any:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    if not stream:
        return await sessions.events(tenant.id, session_id, after_seq=after_seq)
    return _sse_response(
        sessions.store, sessions.event_hub, tenant.id, session_id, after_seq
    )


@router.get("/v1/chat/sessions/{session_id}/export")
async def export_chat_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    return await sessions.export(tenant.id, session_id)


@router.get("/v1/chat/sessions/{session_id}/turns")
async def list_chat_session_turns(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    return await sessions.list_turns(tenant.id, session_id)


@router.get("/v1/chat/sessions/{session_id}/turns/{turn_id}")
async def read_chat_session_turn(
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    return await sessions.get_turn(tenant.id, session_id, turn_id)


@router.get("/v1/chat/sessions/{session_id}/items")
async def list_chat_session_items(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    sessions = _sessions(request)
    _public(await sessions.get(tenant.id, session_id))
    return await sessions.list_items(tenant.id, session_id)
