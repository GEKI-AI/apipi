import asyncio
import contextlib
import logging
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from apipi.common.dirs import session_workspace, wipe_workspace
from apipi.common.errors import ApiError, ObjectStoreError
from apipi.common.event_bus import EventBus, is_wake
from apipi.common.failures import error_extra
from apipi.common.idle import (
    metadata_has_idle_ttl,
    normalize_idle_ttl,
    validate_idle_metadata,
)
from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics
from apipi.common.models import require_model
from apipi.common.otel import Tracing, set_span, start_span
from apipi.common.pi_metadata import (
    THINKING_KEY,
    apply_reasoning_effort,
    copy_inline_pi_metadata,
    public_metadata,
    reasoning_body,
    reject_client_thinking_key,
    reject_codemode_without_builtin_tools,
    reject_reasoning_conflict,
    require_thinking_supported,
    resolve_thinking,
    thinking_from_metadata,
    thinking_to_effort,
    validate_pi_metadata,
)
from apipi.common.sandbox import (
    mem_mib_for_size,
    reject_removed_size_key,
    require_image_size,
    require_known_image,
    resolve_sandbox_image,
    resolve_sandbox_size,
    sandbox_size_of,
    strip_removed_size_key,
)
from apipi.common.skills import copy_capability_directories
from apipi.common.usage import usage_from
from apipi.config import Settings
from apipi.env.setup import (
    SetupError,
    prepare_workspace,
    reject_microvm_system_packages,
)
from apipi.env.spec import EnvironmentSpec, environment_payload
from apipi.gateway.auth import AuthIdentity, not_found
from apipi.gateway.content import (
    ImagePart,
    UserContent,
    parse_user_content,
    require_image_model,
)
from apipi.gateway.errors import gone
from apipi.gateway.tokens import hash_token
from apipi.mcp.guard import check_mcp_url, split_allow_hosts
from apipi.mcp.http import (
    McpConnectError,
    McpHttpServer,
    apply_vault_headers,
    mcp_http_tools,
)
from apipi.services.agents import (
    AgentWrite,
    definition_for_session,
    reject_colliding_mcp_labels,
)
from apipi.services.env_none import (
    is_env_none,
    reject_builtin_tools_for_env_none,
    validate_env_none,
)
from apipi.services.files import FileService
from apipi.services.model_credentials import ModelCredentials
from apipi.services.search import SearchResolver, require_search
from apipi.services.session_defaults import merge_session_create, require_default_refs
from apipi.services.session_events import event_body, persist_event
from apipi.services.skill_store import SkillService
from apipi.services.turn_context import build_turn_context, input_image_ref
from apipi.services.turn_state import fail_session, fail_stale_in_progress
from apipi.services.vault_crypto import (
    VaultCryptoError,
    decrypt_vault_token,
    vault_aad,
    vault_key_bytes,
)
from apipi.services.worker_artifacts import wipe_artifact_store
from apipi.store.blobs import ArtifactBlobs, blob_key
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
from apipi.workerhub.execution import RemoteExecution

log = logging.getLogger("apipi")


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


async def _expire_sandbox(
    db: Any, hub: EventBus, tenant_id: uuid.UUID, row: SessionRow
) -> None:
    from apipi.services.sandbox_status import expire_if_stale

    await expire_if_stale(db, hub, tenant_id, row)


def session_body(row: SessionRow) -> dict[str, Any]:
    from apipi.services.sandbox_status import overlay_environment

    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id) if row.agent_id is not None else None,
        "status": row.status,
        "environment": overlay_environment(row),
        "idle_ttl": row.idle_ttl,
        "metadata": public_metadata(row.metadata_json),
        "reasoning": reasoning_body(row.metadata_json),
        "required_actions": row.required_actions,
        "user_id": row.user_id,
        "org_id": row.org_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "vault_ids": [str(item) for item in (row.vault_ids or [])],
    }


class _VaultPlain:
    def __init__(self, cred_id: uuid.UUID, mcp_server_url: str, token: str) -> None:
        self.id = cred_id
        self.mcp_server_url = mcp_server_url
        self.token = token


def _plain_vault_creds(settings: Settings, creds: list[Any]) -> list[_VaultPlain]:
    key = vault_key_bytes(settings.vault_master_key)
    plain: list[_VaultPlain] = []
    for cred in creds:
        try:
            token = decrypt_vault_token(
                cred.token, key, aad=vault_aad(cred.tenant_id, cred.id)
            )
        except VaultCryptoError as exc:
            raise McpConnectError("vault credential decrypt failed") from exc
        plain.append(_VaultPlain(cred.id, cred.mcp_server_url, token))
    return plain


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


_STREAM_END = frozenset({"agent.session.failed"})


def _stream_ended(event: dict[str, Any] | Any) -> bool:
    if isinstance(event, dict):
        return event.get("type") in _STREAM_END
    return getattr(event, "type", None) in _STREAM_END


async def iter_session_events(
    store: Store,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    after_seq: int | None,
    *,
    ping: bool = False,
    fallback_poll: timedelta | float | None = None,
    metrics: Metrics | None = None,
) -> AsyncGenerator[dict[str, Any] | None]:
    """Stream stored events, then stay current with bus wakes.

    After the replay, the loop waits for a wake instead of polling: a
    wake (or the fallback poll below) triggers one ``after_seq`` read.
    An idle stream therefore queries the database no more often than
    the fallback interval (3s by default). Full bodies delivered to
    the publishing process and live deltas are yielded directly.
    """
    if fallback_poll is None:
        interval = 3.0
    elif isinstance(fallback_poll, timedelta):
        interval = max(fallback_poll.total_seconds(), 0.01)
    else:
        interval = max(float(fallback_poll), 0.01)
    queue = hub.subscribe(session_id)
    try:
        async with store.session() as db:
            existing = await list_events(db, tenant_id, session_id, after_seq=after_seq)
        last = after_seq or 0
        ping_at = 0.0

        async def _read_new() -> list[Any]:
            async with store.session() as db:
                return await list_events(db, tenant_id, session_id, after_seq=last)

        for event in existing:
            last = event.seq
            body = event_body(event)
            yield body
            if _stream_ended(body):
                return
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                payload = None
            if payload is None:
                for event in await _read_new():
                    last = event.seq
                    body = event_body(event)
                    yield body
                    if _stream_ended(body):
                        return
                ping_at += interval
                if ping and ping_at >= 15:
                    ping_at = 0.0
                    yield None
                continue
            if is_wake(payload):
                seq = payload.get("seq")
                if not isinstance(seq, int) or seq <= last:
                    continue
                extra = await _read_new()
                if extra:
                    published = payload.get("published_at")
                    if metrics is not None and isinstance(published, (int, float)):
                        metrics.observe_wake_sse(time.time() - published)
                    for event in extra:
                        last = event.seq
                        body = event_body(event)
                        yield body
                        if _stream_ended(body):
                            return
                ping_at = 0.0
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
            if _stream_ended(payload):
                return
    finally:
        hub.unsubscribe(session_id, queue)


_MAYBE_SENT = frozenset({"forward_timeout", "command_ack_timeout", "forward_failed"})


def _turn_not_sent(exc: BaseException) -> bool:
    """True when the turn command surely never reached a worker."""
    if isinstance(exc, ApiError):
        return exc.code not in _MAYBE_SENT
    return isinstance(exc, ObjectStoreError)


class SessionService:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        event_hub: EventBus,
        execution: RemoteExecution,
        blobs: ArtifactBlobs,
        files: FileService,
        skill_store: SkillService,
        tracing: Tracing | None,
        metrics: Metrics | None = None,
        search: SearchResolver | None = None,
        model_credentials: ModelCredentials | None = None,
    ) -> None:
        self.settings = settings
        self.model_credentials = (
            model_credentials
            if model_credentials is not None
            else ModelCredentials(settings)
        )
        self.search = search if search is not None else SearchResolver(settings)
        self.store = store
        self.event_hub = event_hub
        self.execution = execution
        self.blobs = blobs
        self.files = files
        self.skill_store = skill_store
        self.tracing = tracing
        self.metrics = metrics
        self._turn_tasks: set[asyncio.Task[None]] = set()

    async def cancel_turns(self) -> None:
        pending = list(self._turn_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _mcp_servers(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> list[McpHttpServer]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                not_found()
            if row.agent_id is not None:
                definition = await definition_for_session(db, tenant_id, row)
                raw = definition.get("tools") if isinstance(definition, dict) else []
            else:
                raw = row.tools if isinstance(row.tools, list) else []
            servers = mcp_http_tools(raw)
            allow_hosts = split_allow_hosts(self.settings.mcp_allow_hosts)
            for server in servers:
                await check_mcp_url(
                    server.server_url,
                    label=server.server_label,
                    allow_hosts=allow_hosts,
                )
            vault_ids = [uuid.UUID(item) for item in (row.vault_ids or []) if item]
            if vault_ids:
                creds = await list_credentials_for_vault_ids(db, tenant_id, vault_ids)
                servers = apply_vault_headers(
                    servers, _plain_vault_creds(self.settings, creds)
                )
            return servers

    async def _turn_parts(
        self,
        tenant_id: uuid.UUID,
        content: UserContent,
        known: dict[str, tuple[str, int]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """The `turn.start` parts with image references, and the new file ids.

        Data URL images are stored as files here. The caller deletes the
        new files when the turn does not start.
        """
        images = await self.files.input_images(tenant_id, content.images, known)
        refs = [self._image_ref(tenant_id, *image) for image in images]
        created = [
            file_id
            for image, (file_id, _mime, _size) in zip(
                content.images, images, strict=True
            )
            if not image.file_id
        ]
        return content.wire_parts(refs), created

    def _image_ref(
        self, tenant_id: uuid.UUID, file_id: str, mime: str, size: int
    ) -> dict[str, Any]:
        return input_image_ref(
            self.settings,
            tenant_id,
            file_id,
            mime_type=mime,
            size_bytes=size,
            objects=self.files.objects,
        )

    async def _drop_files(self, tenant_id: uuid.UUID, file_ids: list[str]) -> None:
        """Delete the image files of a turn that did not start, best effort."""
        for file_id in file_ids:
            with contextlib.suppress(Exception):
                await self.files.delete(tenant_id, file_id)
        file_ids.clear()

    async def forward_image_parts(
        self, tenant_id: uuid.UUID, parts: list[Any]
    ) -> list[dict[str, Any]]:
        """Sign the image parts of a forwarded `turn.start` on this replica.

        The forward row keeps only `file_id`. The file is checked for the
        tenant again and the reference is built from the tenant here.
        """
        images = tuple(
            ImagePart(file_id=str(part.get("file_id") or ""))
            for part in parts
            if isinstance(part, dict) and part.get("type") == "image"
        )
        if any(not image.file_id for image in images):
            raise ApiError(
                "invalid_request",
                "forwarded image part has no file_id",
                code="invalid_request",
            )
        known = await self.files.image_files(tenant_id, images)
        out: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "image":
                file_id = str(part["file_id"])
                out.append(self._image_ref(tenant_id, file_id, *known[file_id]))
            else:
                out.append(part)
        return out

    async def _turn_context(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        servers: list[McpHttpServer] | None,
        *,
        api_key: str | None,
        key_id: str | None,
        user_id: str | None,
        org_id: str | None,
    ) -> dict[str, Any]:
        """Build the worker command context from the database and vault."""
        return await build_turn_context(
            self.store,
            self.settings,
            tenant_id,
            session_id,
            mcp_servers=servers,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            objects=self.files.objects,
            search=self.search,
        )

    async def forward_context(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        key_id: str | None,
        user_id: str | None,
        org_id: str | None,
    ) -> dict[str, Any]:
        """The command context for a turn another replica forwarded to this one.

        The request bearer never leaves the replica that took the request.
        The model key comes from the identity the forward row carries.
        """
        identity = AuthIdentity(
            key_id=key_id or "",
            tenant_id=tenant_id,
            user_id=user_id,
            org_id=org_id,
        )
        api_key = await self.model_credentials.resolve(identity, None)
        servers = await self._mcp_servers(tenant_id, session_id)
        return await self._turn_context(
            tenant_id,
            session_id,
            servers,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
        )

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
        log_event(
            log,
            logging.WARNING,
            "worker assign failed",
            event="worker.assign.failed",
            error_code=code,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        raise ApiError(
            "invalid_request",
            message,
            code=code,
            status_code=429,
        )

    async def _raise_if_first_turn_failed(
        self, tenant_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        async with self.store.session() as db:
            turns = await list_turns(db, tenant_id, session_id)
            if not turns:
                return
            last = turns[-1]
            if last.status != "failed":
                return
            code = "model_host_error"
            message = "Model host error"
            extra: dict[str, Any] = {}
            events = await list_events(db, tenant_id, session_id)
            for event in reversed(events):
                if event.type != "agent.session.error":
                    continue
                data = event.data if isinstance(event.data, dict) else {}
                raw_code = data.get("code")
                raw_message = data.get("message")
                if isinstance(raw_code, str) and raw_code:
                    code = raw_code
                if isinstance(raw_message, str) and raw_message:
                    message = raw_message
                extra = error_extra(data)
                break
        raise ApiError(
            "api_error",
            message,
            code=code,
            status_code=502,
            session_id=str(session_id),
            extra=extra,
        )

    async def _saved_defaults(
        self,
        tenant_id: uuid.UUID,
        agent: AgentWrite | None,
        agent_id: uuid.UUID | None,
    ) -> dict[str, Any] | None:
        if agent_id is not None:
            async with self.store.session() as db:
                saved = await get_agent(db, tenant_id, agent_id)
                if saved is None:
                    not_found()
                if isinstance(saved.session_defaults, dict):
                    return saved.session_defaults
            return None
        if agent is None or agent.session_defaults is None:
            return None
        return agent.session_defaults.model_dump(mode="json", exclude_none=True)

    async def create(
        self,
        tenant_id: uuid.UUID,
        *,
        agent: AgentWrite | None = None,
        agent_id: uuid.UUID | None = None,
        environment: EnvironmentSpec | None = None,
        input: str | dict[str, Any] | list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        idle_ttl: str | None = None,
        vault_ids: list[uuid.UUID] | None = None,
        inherit_agent_defaults: bool = True,
        key_id: str = "",
        user_id: str | None = None,
        org_id: str | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
        wait_turn: bool = True,
    ) -> dict[str, Any]:
        pending: list[str] = []
        try:
            return await self._create(
                tenant_id,
                agent=agent,
                agent_id=agent_id,
                environment=environment,
                input=input,
                metadata=metadata,
                idle_ttl=idle_ttl,
                vault_ids=vault_ids,
                inherit_agent_defaults=inherit_agent_defaults,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
                request_id=request_id,
                api_key=api_key,
                wait_turn=wait_turn,
                pending=pending,
            )
        finally:
            await self._drop_files(tenant_id, pending)

    async def _create(
        self,
        tenant_id: uuid.UUID,
        *,
        agent: AgentWrite | None,
        agent_id: uuid.UUID | None,
        environment: EnvironmentSpec | None,
        input: str | dict[str, Any] | list[Any] | None,
        metadata: dict[str, Any] | None,
        idle_ttl: str | None,
        vault_ids: list[uuid.UUID] | None,
        inherit_agent_defaults: bool,
        key_id: str,
        user_id: str | None,
        org_id: str | None,
        request_id: str | None,
        api_key: str | None,
        wait_turn: bool,
        pending: list[str],
    ) -> dict[str, Any]:
        """Create the session. `pending` holds image files no turn owns yet."""
        if agent is None and agent_id is None:
            raise ApiError(
                "invalid_request",
                "Provide agent or agent_id",
                code="invalid_request",
            )
        reject_removed_size_key(metadata)
        reject_client_thinking_key(metadata)
        if agent is not None and agent.metadata is not None:
            reject_removed_size_key(agent.metadata)
            reject_client_thinking_key(agent.metadata)
        agent_defaults = await self._saved_defaults(tenant_id, agent, agent_id)
        if inherit_agent_defaults and agent_defaults:
            label = f"agent {agent_id}" if agent_id is not None else "inline agent"
            async with self.store.session() as db:
                await require_default_refs(
                    db,
                    tenant_id,
                    agent_defaults,
                    agent_label=label,
                    dangling=True,
                )
        environment, vault_ids, agent_size, agent_image = merge_session_create(
            agent_defaults=agent_defaults if inherit_agent_defaults else None,
            environment=environment,
            vault_ids=vault_ids,
            inherit=inherit_agent_defaults,
        )
        env = environment_payload(environment)
        extra_files: list[tuple[str, bytes]] = []
        if env.get("type") == "openai_hosted":
            try:
                extra_files = await self.files.workspace_files(tenant_id, env)
            except ObjectStoreError as exc:
                raise ApiError(
                    "api_error",
                    "Artifact store unavailable",
                    code="artifact_store",
                    status_code=503,
                ) from exc
        turn_content = parse_user_content(input, settings=self.settings)
        image_files = await self.files.image_files(tenant_id, turn_content.images)
        turn_parts, created = await self._turn_parts(
            tenant_id, turn_content, image_files
        )
        pending.extend(created)
        raw_tools: list[Any] = []
        model: str | None = None
        instructions: str | None = None
        agent_metadata: dict[str, Any] | None = None
        async with self.store.session() as db:
            if agent_id is not None:
                saved = await get_agent(db, tenant_id, agent_id)
                if saved is None:
                    not_found()
                raw_tools = saved.tools
                model = saved.model
                agent_metadata = strip_removed_size_key(saved.metadata_json)
            if agent is not None:
                if agent.model is not None:
                    model = agent.model
                if agent.instructions is not None:
                    instructions = agent.instructions
                if agent.tools is not None:
                    raw_tools = [
                        tool.model_dump(exclude_none=True) for tool in agent.tools
                    ]
                if agent.reasoning is not None or agent.metadata:
                    base = agent.metadata if agent.metadata is not None else metadata
                    effort = None if agent.reasoning is None else agent.reasoning.effort
                    reset = (
                        agent.reasoning is not None
                        and "effort" in agent.reasoning.model_fields_set
                        and agent.reasoning.effort is None
                    )
                    if agent.reasoning is not None:
                        reject_reasoning_conflict(base, effort)
                    metadata = apply_reasoning_effort(base, effort, reset=reset)
                    if agent_id is None:
                        agent_metadata = metadata
            if agent_id is None:
                metadata = copy_inline_pi_metadata(metadata, agent_metadata)
            validate_pi_metadata(metadata)
            validate_pi_metadata(agent_metadata)
            if is_env_none(env):
                validate_env_none(metadata, agent_metadata, raw_tools)
            else:
                reject_codemode_without_builtin_tools(metadata, agent_metadata)
            if agent is not None and agent.tools is not None:
                reject_colliding_mcp_labels(raw_tools)
                await require_search(
                    self.search,
                    raw_tools,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    org_id=org_id,
                )
            require_thinking_supported(
                self.settings,
                model,
                thinking_from_metadata(metadata)
                or thinking_from_metadata(agent_metadata),
            )
            validate_idle_metadata(metadata)
            validate_idle_metadata(agent_metadata)
            idle_ttl = normalize_idle_ttl(idle_ttl)
            if (
                idle_ttl is None
                and agent_id is None
                and agent is not None
                and agent.idle_ttl is not None
                and not metadata_has_idle_ttl(metadata)
            ):
                idle_ttl = normalize_idle_ttl(agent.idle_ttl)
            sandbox_agent_metadata = agent_metadata if inherit_agent_defaults else None
            size = resolve_sandbox_size(
                environment_size=env.get("sandbox_size")
                if isinstance(env.get("sandbox_size"), str)
                else None,
                agent_default=agent_size,
                default=self.settings.sandbox_default_size,
            )
            image = resolve_sandbox_image(
                environment_image=env.get("sandbox_image")
                if isinstance(env.get("sandbox_image"), str)
                else None,
                session_metadata=metadata,
                agent_metadata=sandbox_agent_metadata,
                agent_default=agent_image,
                size=size,
                default=self.settings.sandbox_default_image,
            )
            require_known_image(self.settings, image)
            require_image_size(image, size)
            env = {**env, "sandbox_size": size, "sandbox_image": image}
            require_image_model(self.settings, model, turn_content)
            try:
                reject_microvm_system_packages(env, run_mode="microvm")
            except SetupError as exc:
                raise ApiError(
                    "invalid_request",
                    exc.message,
                    code="invalid_request",
                ) from exc
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
                idle_ttl=idle_ttl,
                environment=env,
                metadata=metadata,
                key_id=key_id,
                user_id=user_id,
                org_id=org_id,
                vault_ids=vault_id_strs,
                tools=raw_tools if agent_id is None else None,
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
                except ObjectStoreError as exc:
                    raise ApiError(
                        "api_error",
                        "Artifact store unavailable",
                        code="artifact_store",
                        status_code=503,
                    ) from exc
                hosted_env_id = uuid.uuid4()
                env = {**env, "directory": str(directory), "id": str(hosted_env_id)}
                row.environment = env
                await create_environment(
                    db,
                    tenant_id,
                    row.id,
                    environment_id=hosted_env_id,
                    key_hash=hash_token(secrets.token_urlsafe(32)),
                    status="disconnected",
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
            session_id = row.id
        with start_span(
            self.tracing,
            "session",
            request_id=request_id,
            session_id=session_id,
            model=model,
        ):
            try:
                servers = mcp_http_tools(raw_tools)
                allow_hosts = split_allow_hosts(self.settings.mcp_allow_hosts)
                for server in servers:
                    await check_mcp_url(
                        server.server_url,
                        label=server.server_label,
                        allow_hosts=allow_hosts,
                    )
                if vault_id_strs:
                    async with self.store.session() as db:
                        creds = await list_credentials_for_vault_ids(
                            db,
                            tenant_id,
                            [uuid.UUID(item) for item in vault_id_strs],
                        )
                    servers = apply_vault_headers(
                        servers, _plain_vault_creds(self.settings, creds)
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
            text = turn_content.text
            if text or turn_content.images:
                require_model(model)
                self._require_capacity(
                    session_id,
                    tenant_id,
                    session_mem_mib=mem_mib_for_size(self.settings, size),
                )

                if wait_turn:
                    try:
                        await self.execution.run_turn(
                            tenant_id,
                            session_id,
                            text,
                            parts=turn_parts,
                            mcp_http=servers,
                            request_id=request_id,
                            api_key=api_key,
                            key_id=key_id or None,
                            user_id=user_id,
                            org_id=org_id,
                            turn_context=await self._turn_context(
                                tenant_id,
                                session_id,
                                servers,
                                api_key=api_key,
                                key_id=key_id or None,
                                user_id=user_id,
                                org_id=org_id,
                            ),
                        )
                    except BaseException as exc:
                        if not _turn_not_sent(exc):
                            pending.clear()
                        raise
                    pending.clear()
                    await self._raise_if_first_turn_failed(tenant_id, session_id)
                else:
                    owned = list(pending)
                    pending.clear()

                    async def _run_first_turn() -> None:
                        try:
                            await self.execution.run_turn(
                                tenant_id,
                                session_id,
                                text,
                                parts=turn_parts,
                                mcp_http=servers,
                                request_id=request_id,
                                api_key=api_key,
                                key_id=key_id or None,
                                user_id=user_id,
                                org_id=org_id,
                                turn_context=await self._turn_context(
                                    tenant_id,
                                    session_id,
                                    servers,
                                    api_key=api_key,
                                    key_id=key_id or None,
                                    user_id=user_id,
                                    org_id=org_id,
                                ),
                            )
                        except Exception as exc:
                            log.exception(
                                "background turn",
                                extra={"session_id": str(session_id)},
                            )
                            if _turn_not_sent(exc):
                                await self._drop_files(tenant_id, owned)
                            if isinstance(exc, ApiError) and exc.code:
                                code = exc.code
                            else:
                                code = "internal"
                            if isinstance(exc, ApiError):
                                message = exc.message
                            else:
                                message = "Turn failed"
                            async with self.store.session() as db:
                                row = await get_session(db, tenant_id, session_id)
                                if row is not None and row.status == "failed":
                                    return
                                await fail_session(
                                    db,
                                    self.event_hub,
                                    tenant_id,
                                    session_id,
                                    message,
                                    code=code,
                                )

                    task = asyncio.create_task(_run_first_turn())
                    self._turn_tasks.add(task)
                    task.add_done_callback(self._turn_tasks.discard)
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
        if env.get("type") == "openai_hosted":
            from apipi.services.sandbox_status import eager_boot_enabled

            if eager_boot_enabled(
                self.settings,
                session_metadata=metadata,
                agent_metadata=agent_metadata,
                session_defaults=agent_defaults,
            ):
                boot_servers = await self._mcp_servers(tenant_id, session_id)
                task = asyncio.create_task(
                    self.execution.boot_hosted(
                        tenant_id,
                        session_id,
                        mcp_http=boot_servers,
                        turn_context=await self._turn_context(
                            tenant_id,
                            session_id,
                            boot_servers,
                            api_key=None,
                            key_id=None,
                            user_id=None,
                            org_id=None,
                        ),
                    )
                )
                self._turn_tasks.add(task)
                task.add_done_callback(self._turn_tasks.discard)
        return payload

    async def list(
        self, tenant_id: uuid.UUID, *, user_id: str | None = None
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_sessions(db, tenant_id, user_id=user_id)
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
                await _expire_sandbox(db, self.event_hub, tenant_id, row)
                out.append(session_body(row))
            return {"data": out}

    async def get(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
            await _expire_sandbox(db, self.event_hub, tenant_id, row)
            return session_body(row)

    async def update(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        metadata: dict[str, Any] | None = None,
        agent: AgentWrite | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        reject_removed_size_key(metadata)
        reject_client_thinking_key(metadata)
        if agent is not None and agent.metadata is not None:
            reject_removed_size_key(agent.metadata)
            reject_client_thinking_key(agent.metadata)
        async with self.store.session() as db:
            current = await get_session(db, tenant_id, session_id, user_id=user_id)
            if current is None:
                not_found()
            merged = dict(current.metadata_json or {})
            if metadata is not None:
                merged = dict(metadata)
                if (
                    THINKING_KEY not in metadata
                    and isinstance(current.metadata_json, dict)
                    and THINKING_KEY in current.metadata_json
                ):
                    merged[THINKING_KEY] = current.metadata_json[THINKING_KEY]
            if agent is not None and agent.model is not None:
                changes["model"] = agent.model
            if agent is not None and agent.reasoning is not None:
                effort = agent.reasoning.effort
                reset = (
                    "effort" in agent.reasoning.model_fields_set
                    and agent.reasoning.effort is None
                )
                if metadata is not None:
                    reject_reasoning_conflict(metadata, effort)
                merged = apply_reasoning_effort(merged, effort, reset=reset)
            if metadata is not None or (
                agent is not None and agent.reasoning is not None
            ):
                validate_pi_metadata(merged)
                validate_idle_metadata(merged)
                agent_meta: dict[str, Any] | None = None
                if current.agent_id is not None:
                    definition = await definition_for_session(db, tenant_id, current)
                    if isinstance(definition, dict) and isinstance(
                        definition.get("metadata"), dict
                    ):
                        agent_meta = definition["metadata"]
                if is_env_none(
                    current.environment
                    if isinstance(current.environment, dict)
                    else None
                ):
                    reject_builtin_tools_for_env_none(merged, agent_meta)
                else:
                    reject_codemode_without_builtin_tools(merged, agent_meta)
                model = changes.get("model", current.model)
                if not isinstance(model, str) and current.agent_id is not None:
                    definition = await definition_for_session(db, tenant_id, current)
                    raw_model = (
                        definition.get("model")
                        if isinstance(definition, dict)
                        else None
                    )
                    if isinstance(raw_model, str):
                        model = raw_model
                require_thinking_supported(
                    self.settings,
                    model if isinstance(model, str) else None,
                    thinking_from_metadata(merged),
                )
                changes["metadata"] = merged
            row = await update_session(
                db, tenant_id, session_id, changes=changes, user_id=user_id
            )
            if row is None:
                not_found()
            return await self._present(db, tenant_id, row)

    async def _present(
        self, db: Any, tenant_id: uuid.UUID, row: SessionRow
    ) -> dict[str, Any]:
        body = session_body(row)
        if thinking_from_metadata(row.metadata_json) is not None:
            return body
        agent_meta = None
        if row.agent_id is not None:
            definition = await definition_for_session(db, tenant_id, row)
            if isinstance(definition, dict) and isinstance(
                definition.get("metadata"), dict
            ):
                agent_meta = definition["metadata"]
        level = resolve_thinking(self.settings, None, agent_meta)
        body["reasoning"] = {"effort": thinking_to_effort(level)}
        return body

    async def delete(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
            if row is None:
                not_found()
            directory = row.environment.get("directory")
            key_id = row.key_id
        await self.execution.teardown(session_id)
        if isinstance(directory, str) and directory:
            wipe_workspace(Path(directory))
        await wipe_artifact_store(self.blobs, tenant_id, key_id, session_id)
        async with self.store.session() as db:
            deleted = await delete_session(db, tenant_id, session_id, user_id=user_id)
            if not deleted:
                not_found()
        return {"id": str(session_id), "deleted": True}

    async def post_event(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        content: str | None = None,
        text: str | None = None,
        raw_input: object | None = None,
        turn_id: uuid.UUID | None = None,
        call_id: str | None = None,
        success: bool | None = None,
        output: str | None = None,
        error: str | None = None,
        key_id: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        request_id: str | None = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        parsed = parse_user_content(
            raw_input
            if raw_input is not None
            else (content if content is not None else text),
            settings=self.settings,
        )
        message = parsed.text
        action = "message"
        stale = False
        cancel_status = ""
        follow_size = "S"
        follow_model: str | None = None
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
                follow_model = row.model
                if not follow_model and row.agent_id is not None:
                    definition = await definition_for_session(db, tenant_id, row)
                    raw_model = (
                        definition.get("model")
                        if isinstance(definition, dict)
                        else None
                    )
                    if isinstance(raw_model, str):
                        follow_model = raw_model
        image_files: dict[str, tuple[str, int]] = {}
        if action == "message":
            require_image_model(self.settings, follow_model, parsed)
            image_files = await self.files.image_files(tenant_id, parsed.images)
        turn_servers: list[McpHttpServer] | None = None
        if action in ("tool", "message"):
            try:
                turn_servers = await self._mcp_servers(tenant_id, session_id)
            except McpConnectError as exc:
                async with self.store.session() as db:
                    await fail_session(
                        db, self.event_hub, tenant_id, session_id, str(exc)
                    )
                    row = await get_session(db, tenant_id, session_id)
                    if row is None:
                        not_found()
                    return session_body(row)
        if action == "cancel":
            await self.execution.cancel(session_id, status=cancel_status, strict=True)
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
                    mcp_http=turn_servers,
                    request_id=request_id,
                    api_key=api_key,
                    key_id=key_id,
                    user_id=user_id,
                    org_id=org_id,
                    turn_context=await self._turn_context(
                        tenant_id,
                        session_id,
                        turn_servers,
                        api_key=api_key,
                        key_id=key_id,
                        user_id=user_id,
                        org_id=org_id,
                    ),
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
                turn_parts, created = await self._turn_parts(
                    tenant_id, parsed, image_files
                )
                try:
                    await self.execution.run_turn(
                        tenant_id,
                        session_id,
                        message,
                        parts=turn_parts,
                        mcp_http=turn_servers,
                        request_id=request_id,
                        api_key=api_key,
                        key_id=key_id,
                        user_id=user_id,
                        org_id=org_id,
                        turn_context=await self._turn_context(
                            tenant_id,
                            session_id,
                            turn_servers,
                            api_key=api_key,
                            key_id=key_id,
                            user_id=user_id,
                            org_id=org_id,
                        ),
                    )
                except Exception as exc:
                    if _turn_not_sent(exc):
                        await self._drop_files(tenant_id, created)
                    raise
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
            fallback_poll=self.settings.event_bus_fallback_poll,
            metrics=self.metrics,
        ):
            if item is not None:
                yield item

    async def events(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        after_seq: int | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
            if row is None:
                not_found()
            events = await list_events(db, tenant_id, session_id, after_seq=after_seq)
            return {"data": [event_body(event) for event in events]}

    async def export(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if await get_session(db, tenant_id, session_id, user_id=user_id) is None:
                not_found()
            turns = await list_turns(db, tenant_id, session_id)
            if turns is None:
                not_found()
            return {"data": [turn_body(turn) for turn in turns]}

    async def get_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if await get_session(db, tenant_id, session_id, user_id=user_id) is None:
                not_found()
            turn = await get_session_turn(db, tenant_id, session_id, turn_id)
            if turn is None:
                not_found()
            return turn_body(turn)

    async def list_items(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if await get_session(db, tenant_id, session_id, user_id=user_id) is None:
                not_found()
            items = await list_items(db, tenant_id, session_id)
            if items is None:
                not_found()
            return {"data": [item_body(item) for item in items]}

    async def list_artifacts(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            if await get_session(db, tenant_id, session_id, user_id=user_id) is None:
                not_found()
            artifacts = await list_artifacts(db, tenant_id, session_id)
            if artifacts is None:
                not_found()
            return {"data": [artifact_body(artifact) for artifact in artifacts]}

    async def artifact_content(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> tuple[bytes, str, str]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
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

    async def artifact_object_id(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> tuple[str, str, str]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
            if row is None:
                not_found()
            artifact = await get_session_artifact(
                db, tenant_id, session_id, artifact_id
            )
            if artifact is None:
                not_found()
            key_id = artifact.key_id
            filename = Path(artifact.path).name
            content_type = artifact.content_type
        object_id = blob_key(tenant_id, key_id, session_id, artifact_id)
        return object_id, filename, content_type

    async def delete_artifact(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_session(db, tenant_id, session_id, user_id=user_id)
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
