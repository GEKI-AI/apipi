import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from apipi.config import Settings
from apipi.env.hub import EnvironmentHub
from apipi.env.setup import SetupError, prepare_workspace
from apipi.env.spec import EnvironmentSpec, environment_payload
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError, gone
from apipi.gateway.otel import Tracing, set_span, start_span
from apipi.gateway.tokens import hash_token
from apipi.mcp.http import (
    McpConnectError,
    apply_vault_headers,
    connect_mcp_http_tools,
)
from apipi.mcp.stdio import start_mcp_stdio_tools, stop_mcp_stdio
from apipi.services.agents import AgentWrite
from apipi.services.files import FileService
from apipi.services.runtime import (
    EventHub,
    event_body,
    fail_session,
    fail_stale_in_progress,
    persist_event,
)
from apipi.services.skill_store import SkillService
from apipi.services.skills import copy_capability_directories
from apipi.services.usage import usage_from
from apipi.store.blobs import ArtifactBlobs
from apipi.store.engine import Store
from apipi.store.events import list_events
from apipi.store.models import Artifact, Item, SessionRow, Turn
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
from apipi.worker.execution import LocalExecution, RemoteExecution
from apipi.worker.pi.artifacts import wipe_artifact_store, wipe_workspace
from apipi.worker.pi.dirs import session_workspace
from apipi.worker.pi.sandbox import (
    mem_mib_for_size,
    merge_playwright,
    require_size_rootfs,
    resolve_sandbox_size,
    sandbox_size_of,
)


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


def input_text(value: str | dict[str, Any] | None) -> str:
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


async def iter_session_events(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None,
    *,
    ping: bool = False,
) -> AsyncIterator[dict[str, Any] | None]:
    queue = hub.subscribe(session_id)
    try:
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id, after_seq=after_seq)
        last = after_seq or 0
        ping_at = 0.0
        for event in existing:
            last = event.seq
            yield event_body(event)
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                async with store.session() as db:
                    extra = await list_events(db, tenant_id, session_id, after_seq=last)
                if extra:
                    for event in extra:
                        last = event.seq
                        yield event_body(event)
                    continue
                ping_at += 0.25
                if ping and ping_at >= 15:
                    ping_at = 0.0
                    yield None
                continue
            ping_at = 0.0
            seq = payload.get("seq")
            if seq is None:
                yield payload
                continue
            seq = int(seq)
            if seq <= last:
                continue
            last = seq
            yield payload
    finally:
        hub.unsubscribe(session_id, queue)


class SessionService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        event_hub: EventHub,
        env_hub: EnvironmentHub,
        execution: LocalExecution | RemoteExecution,
        blobs: ArtifactBlobs,
        files: FileService,
        skill_store: SkillService,
        tracing: Tracing | None,
        mcp_http: dict[uuid.UUID, Any],
        mcp_stdio: dict[uuid.UUID, Any],
    ) -> None:
        self.settings = settings
        self.store = store
        self.event_hub = event_hub
        self.env_hub = env_hub
        self.execution = execution
        self.blobs = blobs
        self.files = files
        self.skill_store = skill_store
        self.tracing = tracing
        self.mcp_http = mcp_http
        self.mcp_stdio = mcp_stdio

    def _require_capacity(
        self,
        session_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        session_mem_mib: int | None = None,
    ) -> None:
        code = self.execution.capacity_code(
            session_id, tenant_id, session_mem_mib=session_mem_mib
        )
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

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        agent: AgentWrite | None = None,
        agent_id: uuid.UUID | None = None,
        environment: EnvironmentSpec | None = None,
        input: str | dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        vault_ids: list[uuid.UUID] | None = None,
        key_id: str = "",
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        if (agent is None) == (agent_id is None):
            raise ApiError(
                "invalid_request",
                "Provide agent or agent_id",
                code="invalid_request",
            )
        env = environment_payload(environment)
        extra_files: list[tuple[str, bytes]] = []
        if env.get("type") == "openai_hosted":
            extra_files = await self.files.workspace_files(tenant_id, env)
        raw_tools: list[Any] = []
        env_key: str | None = None
        env_id: uuid.UUID | None = None
        model: str | None = None
        instructions: str | None = None
        async with self.store.session() as db:
            agent_metadata: dict[str, Any] | None = None
            if agent_id is not None:
                saved = await get_agent(db, tenant_id, agent_id)
                if saved is None:
                    not_found()
                raw_tools = saved.tools
                model = saved.model
                agent_metadata = saved.metadata_json
            elif agent is not None:
                model = agent.model
                instructions = agent.instructions
                agent_metadata = agent.metadata
                if agent.tools is not None:
                    raw_tools = [
                        tool.model_dump(exclude_none=True) for tool in agent.tools
                    ]
            size = resolve_sandbox_size(
                environment_size=env.get("sandbox_size")
                if isinstance(env.get("sandbox_size"), str)
                else None,
                session_metadata=metadata,
                agent_metadata=agent_metadata,
                default=self.settings.sandbox_default_size,
            )
            env = {**env, "sandbox_size": size}
            require_size_rootfs(self.settings, size)
            vault_id_strs = [str(item) for item in (vault_ids or [])]
            for vault_id in vault_ids or []:
                if await get_vault(db, tenant_id, vault_id) is None:
                    not_found()
            row = await create_session(
                db,
                tenant_id,
                agent_id=agent_id,
                model=model if agent_id is None else None,
                instructions=instructions if agent_id is None else None,
                environment=env,
                metadata=metadata,
                key_id=key_id,
                vault_ids=vault_id_strs,
            )
            if env.get("type") == "openai_hosted":
                directory = session_workspace(self.settings, tenant_id, row.id)
                caps = env.get("capability_directories")
                if isinstance(caps, list):
                    copy_capability_directories(
                        directory, [item for item in caps if isinstance(item, str)]
                    )
                try:
                    prepare_workspace(
                        directory,
                        env,
                        max_bytes=self.settings.max_workspace_bytes,
                        extra_files=extra_files,
                    )
                    await self.skill_store.install(tenant_id, env, directory)
                except SetupError as exc:
                    raise ApiError(
                        "invalid_request",
                        exc.message,
                        code="invalid_request",
                    ) from exc
                env = {**env, "directory": str(directory)}
                row.environment = env
                await db.flush()
            elif env.get("type") == "self_hosted":
                env_id = uuid.uuid4()
                env_key = secrets.token_urlsafe(32)
                env = {**env, "id": str(env_id)}
                row.environment = env
                row.required_actions = [
                    {
                        "type": "environment_connection",
                        "environment_id": str(env_id),
                    }
                ]
                await create_environment(
                    db,
                    tenant_id,
                    row.id,
                    environment_id=env_id,
                    key_hash=hash_token(env_key),
                )
                await db.flush()
            await persist_event(
                db,
                self.event_hub,
                tenant_id,
                row.id,
                type="agent.session.created",
                data={"id": str(row.id)},
            )
            if env_id is not None:
                await persist_event(
                    db,
                    self.event_hub,
                    tenant_id,
                    row.id,
                    type="agent.session.environment.pending",
                    data={"environment_id": str(env_id)},
                )
            session_id = row.id
        with start_span(
            self.tracing,
            "session",
            request_id=request_id,
            session_id=session_id,
            model=model,
        ):
            try:
                connected = await connect_mcp_http_tools(raw_tools)
                if vault_id_strs:
                    async with self.store.session() as db:
                        creds = await list_credentials_for_vault_ids(
                            db,
                            tenant_id,
                            [uuid.UUID(item) for item in vault_id_strs],
                        )
                    connected = apply_vault_headers(connected, creds)
                stdio = await start_mcp_stdio_tools(
                    merge_playwright(raw_tools, size=size, settings=self.settings),
                    on_host=self.execution.stdio_on_host,
                )
            except McpConnectError as exc:
                async with self.store.session() as db:
                    await fail_session(
                        db, self.event_hub, tenant_id, session_id, str(exc)
                    )
                    row = await get_session(db, tenant_id, session_id)
                    if row is None:
                        not_found()
                    set_span(self.tracing, status="failed")
                    return session_body(row)
            self.mcp_http[session_id] = connected
            self.mcp_stdio[session_id] = stdio
            self.execution.put_stdio(session_id, stdio)
            text = input_text(input)
            if text:
                self._require_capacity(
                    session_id,
                    tenant_id,
                    session_mem_mib=mem_mib_for_size(self.settings, size),
                )
                await self.execution.run_turn(
                    tenant_id,
                    session_id,
                    text,
                    mcp_http=connected,
                    mcp_stdio=stdio,
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id or None,
                )
            else:
                async with self.store.session() as db:
                    await persist_event(
                        db,
                        self.event_hub,
                        tenant_id,
                        session_id,
                        type="agent.session.idle",
                    )
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            payload = session_body(row)
            if env_id is not None and env_key is not None:
                payload["environment_id"] = str(env_id)
                payload["key"] = env_key
        return payload

    async def list(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_sessions(db, tenant_id)
            out: list[dict[str, Any]] = []
            for row in rows:
                if (
                    row.status == "in_progress"
                    and self.event_hub.turn_abort(row.id) is None
                ):
                    recovered = await fail_stale_in_progress(
                        db, self.event_hub, tenant_id, row.id
                    )
                    row = recovered if recovered is not None else row
                out.append(session_body(row))
            return {"data": out}

    async def get(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            if (
                row.status == "in_progress"
                and self.event_hub.turn_abort(session_id) is None
            ):
                recovered = await fail_stale_in_progress(
                    db, self.event_hub, tenant_id, session_id
                )
                if recovered is not None:
                    row = recovered
            return session_body(row)

    async def update(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if metadata is not None:
            changes["metadata"] = metadata
        async with self.store.session() as db:
            row = await update_session(db, tenant_id, session_id, changes=changes)
            if row is None:
                not_found()
            return session_body(row)

    async def delete(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            env_id_raw = row.environment.get("id")
            directory = row.environment.get("directory")
            key_id = row.key_id
            deleted = await delete_session(db, tenant_id, session_id)
            if not deleted:
                not_found()
        if isinstance(env_id_raw, str):
            await self.env_hub.close(uuid.UUID(env_id_raw))
        await self.execution.teardown(session_id)
        if isinstance(directory, str) and directory:
            wipe_workspace(Path(directory))
        await wipe_artifact_store(self.blobs, tenant_id, key_id, session_id)
        self.mcp_http.pop(session_id, None)
        stdio = self.mcp_stdio.pop(session_id, None)
        if stdio:
            await stop_mcp_stdio(stdio)
        return {"id": str(session_id), "deleted": True}

    async def post_event(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        content: str | None = None,
        text: str | None = None,
        turn_id: uuid.UUID | None = None,
        call_id: str | None = None,
        success: bool | None = None,
        output: str | None = None,
        error: str | None = None,
        key_id: str | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        message = content if content is not None else text
        if message is None:
            message = ""
        action = "message"
        stale = False
        cancel_status = ""
        follow_size = "S"
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            follow_size = sandbox_size_of(row.environment)
            if type == "agent.session.input.cancel":
                action = "cancel"
                cancel_status = row.status
            elif type == "agent.session.input.tool_result":
                if turn_id is None or call_id is None or success is None:
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
        if action == "cancel":
            await self.execution.cancel(session_id, status=cancel_status)
        elif action == "tool":
            if turn_id is None or call_id is None or success is None:
                raise ApiError(
                    "invalid_request",
                    "tool_result needs turn_id, call_id, and success",
                    code="invalid_request",
                )
            with start_span(
                self.tracing,
                "session",
                request_id=request_id,
                session_id=session_id,
            ):
                await self.execution.continue_turn(
                    tenant_id,
                    session_id,
                    turn_id=turn_id,
                    call_id=call_id,
                    success=success,
                    output=output,
                    error=error,
                    mcp_http=self.mcp_http.get(session_id),
                    mcp_stdio=self.mcp_stdio.get(session_id),
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id,
                )
        else:
            if stale:
                await self.execution.prepare_for_new_turn(tenant_id, session_id)
            with start_span(
                self.tracing,
                "session",
                request_id=request_id,
                session_id=session_id,
            ):
                self._require_capacity(
                    session_id,
                    tenant_id,
                    session_mem_mib=mem_mib_for_size(self.settings, follow_size),
                )
                await self.execution.run_turn(
                    tenant_id,
                    session_id,
                    message,
                    mcp_http=self.mcp_http.get(session_id),
                    mcp_stdio=self.mcp_stdio.get(session_id),
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id,
                )
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            return session_body(row)

    async def stream(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        after_seq: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for item in iter_session_events(
            self.store,
            self.event_hub,
            tenant_id,
            session_id,
            after_seq,
        ):
            if item is not None:
                yield item

    async def events(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        after_seq: int | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            events = await list_events(db, tenant_id, session_id, after_seq=after_seq)
            return {"data": [event_body(event) for event in events]}

    async def export(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            events = await list_events(db, tenant_id, session_id)
            turns = await list_turns(db, tenant_id, session_id)
            items = await list_items(db, tenant_id, session_id)
            if turns is None or items is None:
                not_found()
            return {
                "events": [event_body(event) for event in events],
                "turns": [turn_body(turn) for turn in turns],
                "items": [item_body(item) for item in items],
            }

    async def list_turns(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            turns = await list_turns(db, tenant_id, session_id)
            if turns is None:
                not_found()
            return {"data": [turn_body(turn) for turn in turns]}

    async def get_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            turn = await get_session_turn(db, tenant_id, session_id, turn_id)
            if turn is None:
                not_found()
            return turn_body(turn)

    async def list_items(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            items = await list_items(db, tenant_id, session_id)
            if items is None:
                not_found()
            return {"data": [item_body(item) for item in items]}

    async def list_artifacts(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            artifacts = await list_artifacts(db, tenant_id, session_id)
            if artifacts is None:
                not_found()
            return {"data": [artifact_body(artifact) for artifact in artifacts]}

    async def artifact_content(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> tuple[bytes, str, str]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            artifact = await get_session_artifact(
                db, tenant_id, session_id, artifact_id
            )
            if artifact is None:
                not_found()
            filename = Path(artifact.path).name
            content_type = artifact.content_type
            key_id = artifact.key_id
        data = await self.blobs.get(tenant_id, key_id, session_id, artifact_id)
        if data is None:
            gone()
        return data, content_type, filename

    async def delete_artifact(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            artifact = await get_session_artifact(
                db, tenant_id, session_id, artifact_id
            )
            if artifact is None:
                not_found()
            key_id = artifact.key_id
            await self.blobs.delete(tenant_id, key_id, session_id, artifact_id)
            deleted = await delete_session_artifact(
                db, tenant_id, session_id, artifact_id
            )
            if deleted is None:
                not_found()
        return {"id": str(artifact_id), "deleted": True}
