import json
import uuid
from collections.abc import AsyncGenerator
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Discriminator, Tag, model_validator
from pydantic_core import PydanticCustomError

from apipi.api.authorize import require_session_agent
from apipi.api.deps import model_key
from apipi.common.event_bus import EventBus
from apipi.env.spec import EnvironmentSpec
from apipi.gateway.auth import check_authorize, require_tenant
from apipi.gateway.request_id import request_id_of
from apipi.gateway.schemas import StrictModel
from apipi.services.agents import AgentWrite
from apipi.services.sessions import SessionService, iter_session_events
from apipi.store.disposition import content_disposition
from apipi.store.engine import Store
from apipi.store.models import Tenant

router = APIRouter()

SSE_PING = ": ping\n"


def _sessions(request: Request) -> SessionService:
    return request.app.state.gateway.sessions


def _key_id(request: Request) -> str:
    value = getattr(request.state, "key_id", None)
    return value if isinstance(value, str) else ""


def _user_id(request: Request) -> str | None:
    value = getattr(request.state, "user_id", None)
    return value if isinstance(value, str) and value else None


def _org_id(request: Request) -> str | None:
    value = getattr(request.state, "org_id", None)
    return value if isinstance(value, str) and value else None


class SessionCreate(StrictModel):
    agent: AgentWrite | None = None
    agent_id: uuid.UUID | None = None
    environment: EnvironmentSpec | None = None
    input: str | dict[str, Any] | list[Any] | None = None
    metadata: dict[str, Any] | None = None
    idle_ttl: str | None = None
    stream: bool = False
    vault_ids: list[uuid.UUID] | None = None
    inherit_agent_defaults: bool = True


class SessionUpdate(StrictModel):
    metadata: dict[str, Any] | None = None
    agent: AgentWrite | None = None


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
    type: Literal["input_text"]
    text: str


class OpenAIInputImage(StrictModel):
    type: Literal["input_image"]
    image_url: str | None = None
    file_id: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def one_source(self) -> Self:
        if bool(self.image_url) == bool(self.file_id):
            raise ValueError("input_image needs image_url or file_id")
        return self


class OpenAIInputOther(BaseModel):
    type: str

    @model_validator(mode="after")
    def unknown_part(self) -> Self:
        raise PydanticCustomError(
            "not_implemented",
            "{field} is not implemented",
            {"field": self.type},
        )


def _part_kind(value: Any) -> str:
    kind = value.get("type") if isinstance(value, dict) else getattr(value, "type", "")
    return kind if kind in ("input_text", "input_image") else "other"


OpenAIInputPart = Annotated[
    Annotated[OpenAIInputText, Tag("input_text")]
    | Annotated[OpenAIInputImage, Tag("input_image")]
    | Annotated[OpenAIInputOther, Tag("other")],
    Discriminator(_part_kind),
]


class OpenAIMessageInput(StrictModel):
    role: str
    content: list[OpenAIInputPart]


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

    def raw_messages(self) -> list[dict[str, Any]] | None:
        event = self.events[0]
        if event.type != "agent.session.input.message" or not event.input:
            return None
        return [item.model_dump(exclude_none=True) for item in event.input]

    def to_session_input(self) -> SessionInput:
        event = self.events[0]
        if event.type == "agent.session.input.message":
            return SessionInput(type=event.type, content=_message_text(event.input))
        return SessionInput(
            type=event.type,
            turn_id=event.turn_id,
            call_id=event.call_id,
            success=event.success,
            output=event.output,
            error=event.error,
        )


def _message_text(messages: list[OpenAIMessageInput] | None) -> str:
    if not messages:
        raise ValueError("message needs input")
    texts: list[str] = []
    saw_image = False
    for message in messages:
        for part in message.content:
            if isinstance(part, OpenAIInputText):
                texts.append(part.text)
            elif isinstance(part, OpenAIInputImage):
                saw_image = True
    if not texts and not saw_image:
        raise ValueError("message needs input_text")
    return "\n".join(texts)


SessionEventBody = OpenAIEventsBody | SessionInput


def _sse(event: dict[str, Any]) -> str:
    prefix = f"id: {event['seq']}\n" if "seq" in event else ""
    return f"{prefix}event: {event['type']}\ndata: {json.dumps(event)}\n\n"


async def _event_stream(
    store: Store,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None,
    *,
    fallback_poll: Any | None = None,
    metrics: Any | None = None,
) -> AsyncGenerator[str]:
    async for item in iter_session_events(
        store,
        hub,
        tenant_id,
        session_id,
        after_seq,
        ping=True,
        fallback_poll=fallback_poll,
        metrics=metrics,
    ):
        if item is None:
            yield SSE_PING
        else:
            yield _sse(item)


def _sse_response(
    store: Store,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None,
    *,
    fallback_poll: Any | None = None,
    metrics: Any | None = None,
) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(
            store,
            hub,
            tenant_id,
            session_id,
            after_seq,
            fallback_poll=fallback_poll,
            metrics=metrics,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/v1/agents/sessions")
async def create_agent_session(
    body: SessionCreate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    if body.agent_id is not None:
        async with request.app.state.store.session() as _db:
            from apipi.store.repo import get_agent as _get_agent

            if await _get_agent(_db, tenant.id, body.agent_id) is None:
                from apipi.gateway.auth import not_found as _nf

                _nf()
    await check_authorize(
        request,
        action="agent.run",
        resource_type="agent",
        resource_id=str(body.agent_id) if body.agent_id is not None else None,
    )
    sessions = _sessions(request)
    payload = await sessions.create(
        tenant.id,
        agent=body.agent,
        agent_id=body.agent_id,
        environment=body.environment,
        input=body.input,
        metadata=body.metadata,
        idle_ttl=body.idle_ttl,
        vault_ids=body.vault_ids,
        inherit_agent_defaults=body.inherit_agent_defaults,
        key_id=_key_id(request),
        user_id=_user_id(request),
        org_id=_org_id(request),
        request_id=request_id_of(request),
        api_key=await model_key(request),
        wait_turn=not body.stream,
    )
    if body.stream:
        return _sse_response(
            sessions.store,
            sessions.event_hub,
            tenant.id,
            uuid.UUID(payload["id"]),
            None,
            fallback_poll=request.app.state.settings.event_bus_fallback_poll,
            metrics=request.app.state.metrics,
        )
    return payload


@router.get("/v1/agents/sessions")
async def list_agent_sessions(
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    filt = await check_authorize(
        request, action="session.list", resource_type="agent", resource_id=None
    )
    payload = await _sessions(request).list(tenant.id, user_id=_user_id(request))
    if filt is not None and filt.ids is not None:
        items = payload.get("data", [])
        payload = dict(payload)
        payload["data"] = [s for s in items if str(s.get("agent_id")) in filt.ids]
    return payload


@router.get("/v1/agents/sessions/{session_id}")
async def read_agent_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).get(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.post("/v1/agents/sessions/{session_id}")
async def update_agent_session(
    session_id: uuid.UUID,
    body: SessionUpdate,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="agent.run", user_id=_user_id(request)
    )
    return await _sessions(request).update(
        tenant.id,
        session_id,
        metadata=body.metadata,
        agent=body.agent,
        user_id=_user_id(request),
    )


@router.delete("/v1/agents/sessions/{session_id}")
async def delete_agent_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="agent.run", user_id=_user_id(request)
    )
    return await _sessions(request).delete(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.post("/v1/agents/sessions/{session_id}/events")
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionEventBody,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    parsed = body.to_session_input() if isinstance(body, OpenAIEventsBody) else body
    raw = body.raw_messages() if isinstance(body, OpenAIEventsBody) else parsed.content
    await require_session_agent(
        request, tenant.id, session_id, action="agent.run", user_id=_user_id(request)
    )
    return await _sessions(request).post_event(
        tenant.id,
        session_id,
        type=parsed.type,
        content=parsed.content,
        text=parsed.text,
        raw_input=raw,
        turn_id=parsed.turn_id,
        call_id=parsed.call_id,
        success=parsed.success,
        output=parsed.output,
        error=parsed.error,
        key_id=_key_id(request) or None,
        user_id=_user_id(request),
        org_id=_org_id(request),
        request_id=request_id_of(request),
        api_key=await model_key(request),
    )


@router.get("/v1/agents/sessions/{session_id}/events")
async def get_session_events(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
    stream: bool = False,
    after_seq: int | None = Query(default=None),
) -> Any:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    sessions = _sessions(request)
    if not stream:
        return await sessions.events(
            tenant.id, session_id, after_seq=after_seq, user_id=_user_id(request)
        )
    await sessions.get(tenant.id, session_id, user_id=_user_id(request))
    return _sse_response(
        sessions.store,
        sessions.event_hub,
        tenant.id,
        session_id,
        after_seq,
        fallback_poll=request.app.state.settings.event_bus_fallback_poll,
        metrics=request.app.state.metrics,
    )


@router.get("/v1/apipi/sessions/{session_id}/export")
async def export_agent_session(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).export(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.get("/v1/agents/sessions/{session_id}/turns")
async def list_session_turns(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).list_turns(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.get("/v1/agents/sessions/{session_id}/turns/{turn_id}")
async def read_session_turn(
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).get_turn(
        tenant.id, session_id, turn_id, user_id=_user_id(request)
    )


@router.get("/v1/agents/sessions/{session_id}/items")
async def list_session_items(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).list_items(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.get("/v1/agents/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    return await _sessions(request).list_artifacts(
        tenant.id, session_id, user_id=_user_id(request)
    )


@router.get("/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/content")
async def read_session_artifact_content(
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> Any:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    data, content_type, filename = await _sessions(request).artifact_content(
        tenant.id, session_id, artifact_id, user_id=_user_id(request)
    )
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/v1/apipi/sessions/{session_id}/artifacts/{artifact_id}/download")
async def download_session_artifact(
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="session.read", user_id=_user_id(request)
    )
    object_id, filename, content_type = await _sessions(request).artifact_object_id(
        tenant.id, session_id, artifact_id, user_id=_user_id(request)
    )
    return request.app.state.gateway.uploads.download(
        "artifacts",
        object_id,
        filename=filename,
        content_type=content_type,
    )


@router.delete("/v1/agents/sessions/{session_id}/artifacts/{artifact_id}")
async def delete_agent_session_artifact(
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
    request: Request,
    tenant: Annotated[Tenant, Depends(require_tenant)],
) -> dict[str, Any]:
    await require_session_agent(
        request, tenant.id, session_id, action="agent.run", user_id=_user_id(request)
    )
    return await _sessions(request).delete_artifact(
        tenant.id, session_id, artifact_id, user_id=_user_id(request)
    )
