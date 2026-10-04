import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

from pydantic import ValidationError

from apipi.common.dirs import pi_session_file
from apipi.common.errors import ApiError, ObjectStoreError
from apipi.common.event_bus import EventBus, InMemoryEventBus, live_event_body
from apipi.common.failures import (
    Failure,
    failure_for,
    failure_from_payload,
    session_failed_data,
)
from apipi.common.ids import uuid_or_none
from apipi.common.metrics import Metrics
from apipi.common.models import require_model
from apipi.common.otel import Tracing, set_span, start_span
from apipi.common.pi_metadata import (
    effective_codemode,
    resolve_builtin_tools,
    resolve_thinking,
)
from apipi.common.sandbox import (
    image_for_size,
    mem_mib_for_size,
    sandbox_image_of,
    sandbox_size_of,
)
from apipi.common.skills import discover_skill_dirs, unpack_skill_zip
from apipi.common.usage import add_usage, empty_usage, mcp_name, usage_from
from apipi.config import CapacityError, Settings
from apipi.env.setup import (
    SetupError,
    hosted_workspace,
    provision_hosted_async,
    session_env_from,
)
from apipi.protocol import (
    LIVE_EVENT_TYPES,
    ContextBytes,
    TurnContext,
    parse_turn_context,
)
from apipi.protocol import PUBLIC_EVENT_TYPES as PUBLIC_EVENT_TYPES
from apipi.worker.outbox import OutboxFull
from apipi.worker.pi.artifacts import (
    ensure_openai_workspace,
)
from apipi.worker.pi.model_host import note_pi_model
from apipi.worker.pi.platform_prompt import compose_instructions
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.settings_json import resolve_system_prompt
from apipi.worker.sink import ResultSink
from apipi.worker.turn_context import (
    fetch_input_images,
    fetch_pi_session_bytes,
    materialize_skill_zips,
    materialize_workspace_files,
    mcp_servers_from_context,
)
from apipi.worker.turn_end import (
    cancel_turn,
    complete_turn,
    emit_item,
    fail_outbox_full,
    fail_turn,
)

log = logging.getLogger("apipi")


class TurnFailed(Exception):
    def __init__(
        self,
        message: str,
        *,
        failure: Failure | None = None,
        code: str = "model_host_error",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.failure = failure or failure_for(code, message)
        self.code = self.failure.code


EventHub = InMemoryEventBus


class Harness(Protocol):
    def generate(
        self,
        text: str,
        *,
        session_id: uuid.UUID | None = None,
        cwd: str | None = None,
        tools: bool = True,
        **kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]: ...

    async def abort(self, session_id: uuid.UUID) -> None: ...


def _cwd_and_tools(
    environment: dict[str, Any],
    builtin_tools: str = "on",
) -> tuple[str | None, bool]:
    env_type = environment.get("type")
    if env_type == "none":
        return None, False
    cwd = environment.get("directory")
    cwd_path = cwd if isinstance(cwd, str) else None
    return cwd_path, builtin_tools == "on"


def _require_turn_context(raw: dict[str, Any] | None) -> TurnContext:
    """Parse the command context; a missing or invalid one fails loudly."""
    if raw is None:
        raise ApiError(
            "internal",
            "Turn context is required",
            code="internal",
            status_code=500,
        )
    try:
        return parse_turn_context(raw)
    except (ContextBytes, ValidationError) as exc:
        raise ApiError(
            "invalid_request",
            f"invalid turn context: {exc}",
            code="invalid_request",
        ) from exc


def _idle_spawn_from_context(
    settings: Settings | None, env_type: str | None, ctx: TurnContext
) -> dict[str, Any]:
    """Spawn idle TTL from the context's resolved effective TTL."""
    if settings is None:
        return {}
    seconds = ctx.session.idle_ttl_seconds
    ttl = (
        timedelta(seconds=seconds)
        if seconds is not None
        else settings.pi_idle_ttl_for(env_type)
    )
    return {"idle_ttl": ttl, "idle_ttl_set": True}


def _pi_spawn_overrides(
    settings: Settings | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    builtin_tools: str | None = None,
) -> dict[str, Any]:
    if settings is None:
        return {}
    if builtin_tools is None:
        builtin_tools = resolve_builtin_tools(session_metadata, agent_metadata)
    return {
        "thinking": resolve_thinking(settings, session_metadata, agent_metadata),
        "system_prompt": resolve_system_prompt(
            settings, session_metadata, agent_metadata
        ),
        "system_prompt_set": True,
        "codemode": effective_codemode(builtin_tools, session_metadata, agent_metadata),
    }


def _skill_dirs(environment: dict[str, Any], builtin_tools: str = "on") -> list[str]:
    if builtin_tools == "off":
        return []
    cwd_path, _tools = _cwd_and_tools(environment)
    workspace = Path(cwd_path) if cwd_path is not None else None
    raw = environment.get("capability_directories")
    directories = None
    if isinstance(raw, list):
        directories = [item for item in raw if isinstance(item, str)]
    return discover_skill_dirs(workspace, directories)


def _new_retry_state() -> dict[str, Any]:
    return {"started": 0, "backoff": False}


def _attempts_so_far(state: dict[str, Any]) -> int:
    started = state.get("started")
    count = started if isinstance(started, int) and started >= 0 else 0
    if state.get("backoff"):
        return max(count, 1)
    return count + 1


def _note_retry(state: dict[str, Any], etype: str, data: dict[str, Any]) -> None:
    if etype == "agent.session.turn.retrying":
        attempt = data.get("attempt")
        if isinstance(attempt, int) and attempt > int(state["started"]):
            state["started"] = attempt
        state["backoff"] = True
        return
    if etype == "agent.session.turn.retry.completed":
        state["backoff"] = False


async def _consume_generate(
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    events: AsyncIterator[tuple[str, dict[str, Any]]],
    retry_state: dict[str, Any] | None = None,
    *,
    sink: ResultSink,
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    reply = ""
    pending: list[dict[str, Any]] = []
    usage = empty_usage()
    state = retry_state if retry_state is not None else _new_retry_state()
    async for etype, data in events:
        if etype == "usage":
            usage = add_usage(usage, usage_from(data))
            continue
        _note_retry(state, etype, data)
        if etype == "pi_error":
            failure = replace(
                failure_from_payload(data),
                upstream_attempts=int(state["started"]) + 1,
            )
            raise TurnFailed(failure.message, failure=failure)
        payload = dict(data)
        payload.setdefault("turn_id", str(turn_id))
        if etype in LIVE_EVENT_TYPES:
            await hub.publish(
                session_id, live_event_body(session_id, type=etype, data=payload)
            )
            continue
        if etype == "function_call":
            call_id = payload.get("call_id")
            name = payload.get("name")
            arguments = payload.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            if not isinstance(arguments, dict):
                arguments = {}
            call = {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
            pending.append(call)
            sink.tally_tool(name)
            await emit_item(
                hub,
                tenant_id,
                session_id,
                turn_id=turn_id,
                type="function_call",
                data=call,
                sink=sink,
            )
            continue
        if (
            etype
            in (
                "agent.session.turn.item.added",
                "agent.session.turn.item.nested",
            )
            and payload.get("item_type") == "mcp_call"
        ):
            sink.tally_mcp(mcp_name(payload))
        if etype == "agent.session.turn.output_text.done":
            text_out = payload.get("text")
            if isinstance(text_out, str):
                reply = text_out
        await sink.append_event(hub, tenant_id, session_id, type=etype, data=payload)
    return reply, pending, usage


def _network_access(environment: dict[str, Any] | None) -> str | None:
    if not isinstance(environment, dict):
        return None
    raw = environment.get("network")
    if not isinstance(raw, dict):
        return None
    access = raw.get("access")
    if access in {"enabled", "restricted"}:
        return access
    return None


async def prepare_for_new_turn(
    hub: EventBus,
    harness: Harness,
    session_id: uuid.UUID,
) -> None:
    abort = hub.turn_abort(session_id)
    if abort is not None:
        abort.set()
        await harness.abort(session_id)


def _bind_turn_model(settings: Settings | None, model: str | None) -> str:
    resolved = require_model(model)
    if settings is not None and settings.model_base_url:
        note_pi_model(settings, resolved)
    return resolved


async def report_session_failed(
    sink: ResultSink,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    await sink.update_session(
        tenant_id,
        session_id,
        changes={"status": "failed", "required_actions": []},
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_failed_data(message, code),
    )
    await sink.append_event(hub, tenant_id, session_id, type="agent.session.failed")


async def report_environment_failed(
    sink: ResultSink,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    data: dict[str, Any] = {"error": message}
    if code:
        data["code"] = code
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.environment.failed",
        data=data,
    )
    await report_session_failed(sink, hub, tenant_id, session_id, message, code=code)


async def load_boot_kwargs(
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    mcp_http: list[Any] | None = None,
    turn_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from apipi.common.sandbox import (
        image_for_size,
        mem_mib_for_size,
        sandbox_image_of,
        sandbox_size_of,
    )
    from apipi.worker.pi.platform_prompt import compose_instructions

    ctx = _require_turn_context(turn_context)
    environment = (
        dict(ctx.session.environment)
        if isinstance(ctx.session.environment, dict)
        else {}
    )
    if environment.get("type") != "openai_hosted":
        return None
    session_metadata = dict(ctx.session.metadata)
    agent_metadata = dict(ctx.agent.metadata)
    row = cast(
        Any,
        SimpleNamespace(
            environment=environment,
            metadata_json=session_metadata,
            key_id=ctx.session.key_id,
            agent_id=uuid_or_none(ctx.session.agent_id),
            user_id=ctx.session.user_id,
            org_id=ctx.session.org_id,
        ),
    )
    ensure_openai_workspace(row.environment)
    gateway_hosts: tuple[str, ...] = ()
    if settings.run_mode == "microvm":
        from apipi.worker.pi.microvm import microvm_egress_hosts

        gateway_hosts = tuple(microvm_egress_hosts(settings))
    extra_files = await materialize_workspace_files(
        [ref.model_dump() for ref in ctx.files],
        settings,
        hosted_workspace(row.environment),
    )
    await provision_hosted_async(
        row.environment,
        run_mode=settings.run_mode,
        max_bytes=settings.max_workspace_bytes,
        gateway_allowlist=settings.microvm_egress_allowlist,
        gateway_hosts=gateway_hosts,
        extra_files=extra_files,
        timeout=settings.turn_timeout.total_seconds(),
    )
    directory = row.environment.get("directory")
    if isinstance(directory, str) and directory:
        for blob in await materialize_skill_zips(
            [ref.model_dump() for ref in ctx.skills], settings
        ):
            unpack_skill_zip(
                Path(directory),
                blob,
                max_bytes=int(settings.max_workspace_bytes),
            )
    builtin = ctx.agent.builtin_tools
    cwd_path, tools = _cwd_and_tools(row.environment, builtin)
    sandbox_size = sandbox_size_of(row.environment)
    stored_image = sandbox_image_of(row.environment)
    sandbox_image = stored_image or image_for_size(sandbox_size)
    composed = compose_instructions(
        settings,
        ctx.agent.instructions,
        env_type="openai_hosted",
        sandbox_size=sandbox_size,
        mem_mib=mem_mib_for_size(settings, sandbox_size),
        network=_network_access(row.environment),
        builtin_tools=builtin,
    )
    resolved_boot_mcp = (
        mcp_http if mcp_http is not None else mcp_servers_from_context(ctx.model_dump())
    )
    kwargs: dict[str, Any] = {
        "cwd": cwd_path,
        "tools": tools,
        "mcp_http": resolved_boot_mcp,
        "skill_dirs": _skill_dirs(row.environment, builtin),
        "tenant_id": tenant_id,
        "model": ctx.agent.model,
        "instructions": composed,
        "key_id": row.key_id,
        "env_type": "openai_hosted",
        "mem_mib": mem_mib_for_size(settings, sandbox_size),
        "image": sandbox_image,
        "extra_env": session_env_from(row.environment),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "user_id": row.user_id,
        "org_id": row.org_id,
    }
    kwargs.update(
        _pi_spawn_overrides(settings, session_metadata, agent_metadata, builtin)
    )
    kwargs.update(_idle_spawn_from_context(settings, "openai_hosted", ctx))
    return kwargs


def _model_span_attrs(
    *,
    request_id: str | None,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    model: str | None,
    usage: dict[str, int],
    status: str,
) -> dict[str, object]:
    stored = usage_from(usage)
    return {
        "request_id": request_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "model": model,
        "status": status,
        "prompt_tokens": stored["prompt_tokens"],
        "completion_tokens": stored["completion_tokens"],
        "cache_read_tokens": stored["cache_read_tokens"],
        "cache_write_tokens": stored["cache_write_tokens"],
        "total_tokens": stored["total_tokens"],
    }


def _user_item_content(
    text: str, parts: list[dict[str, Any]]
) -> str | list[dict[str, Any]]:
    if not any(part.get("type") == "image" for part in parts):
        return text
    stored: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") == "input_text":
            stored.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image":
            stored.append({"type": "input_image", "file_id": part.get("file_id")})
    return stored


def _spawn_identity_empty(user_id: str | None, org_id: str | None) -> dict[str, Any]:
    return {"agent_id": None, "user_id": user_id, "org_id": org_id}


def _spawn_identity(
    row: Any, user_id: str | None, org_id: str | None
) -> dict[str, Any]:
    stored_org = getattr(row, "org_id", None)
    stored_user = getattr(row, "user_id", None)
    agent = getattr(row, "agent_id", None)
    return {
        "agent_id": str(agent) if agent is not None else None,
        "user_id": user_id if user_id is not None else stored_user,
        "org_id": org_id if org_id is not None else stored_org,
    }


async def run_turn(
    hub: EventBus,
    harness: Harness,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    text: str,
    *,
    parts: list[dict[str, Any]] | None = None,
    mcp_http: list[Any] | None = None,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    turn_timeout: timedelta | None = None,
    settings: Settings | None = None,
    pool: PiPool | None = None,
    api_key: str | None = None,
    key_id: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    turn_context: dict[str, Any] | None = None,
    sink: ResultSink,
) -> None:
    abort = hub.watch_turn(session_id)
    if pool is not None:
        pool.hold(session_id)
    try:
        ctx = _require_turn_context(turn_context)
        ordered = parts or []
        item_content = _user_item_content(text, ordered)
        images: list[dict[str, str]] | None = None
        if settings is not None and isinstance(item_content, list):
            try:
                images = await fetch_input_images(ordered, settings)
            except ObjectStoreError:
                await report_environment_failed(
                    sink,
                    hub,
                    tenant_id,
                    session_id,
                    "Cannot read input images",
                    code="artifact_store",
                )
                return
        resolved_key = api_key if api_key is not None else ctx.model.api_key
        resolved_mcp = (
            mcp_http
            if mcp_http is not None
            else mcp_servers_from_context(ctx.model_dump())
        )
        resolved_base_url = ctx.model.base_url or None
        environment = (
            dict(ctx.session.environment)
            if isinstance(ctx.session.environment, dict)
            else {}
        )
        session_metadata = dict(ctx.session.metadata)
        agent_metadata = dict(ctx.agent.metadata)
        function_tools = [dict(item) for item in ctx.agent.function_tools]
        model: str | None = ctx.agent.model
        instructions = ctx.agent.instructions
        row = cast(
            Any,
            SimpleNamespace(
                environment=environment,
                metadata_json=session_metadata,
                key_id=ctx.session.key_id,
                agent_id=uuid_or_none(ctx.session.agent_id),
                user_id=ctx.session.user_id,
                org_id=ctx.session.org_id,
            ),
        )
        ensure_openai_workspace(row.environment)
        try:
            model = _bind_turn_model(settings, model)
            gateway_allowlist = False
            gateway_hosts: tuple[str, ...] = ()
            extra_files: list[tuple[str, bytes]] = []
            if settings is not None:
                gateway_allowlist = settings.microvm_egress_allowlist
                if settings.run_mode == "microvm":
                    from apipi.worker.pi.microvm import microvm_egress_hosts

                    gateway_hosts = tuple(microvm_egress_hosts(settings))
                extra_files = await materialize_workspace_files(
                    [ref.model_dump() for ref in ctx.files],
                    settings,
                    hosted_workspace(row.environment),
                )
            await provision_hosted_async(
                row.environment,
                run_mode=settings.run_mode if settings is not None else "none",
                max_bytes=(
                    settings.max_workspace_bytes if settings is not None else None
                ),
                gateway_allowlist=gateway_allowlist,
                gateway_hosts=gateway_hosts,
                extra_files=extra_files,
                timeout=(
                    settings.turn_timeout.total_seconds()
                    if settings is not None
                    else None
                ),
            )
            if settings is not None:
                directory = row.environment.get("directory")
                if isinstance(directory, str) and directory:
                    for blob in await materialize_skill_zips(
                        [ref.model_dump() for ref in ctx.skills], settings
                    ):
                        unpack_skill_zip(
                            Path(directory),
                            blob,
                            max_bytes=int(settings.max_workspace_bytes),
                        )
        except (SetupError, ApiError) as exc:
            code = exc.code if isinstance(exc, ApiError) and exc.code else None
            await report_environment_failed(
                sink, hub, tenant_id, session_id, exc.message, code=code
            )
            return
        except ObjectStoreError:
            await report_environment_failed(
                sink,
                hub,
                tenant_id,
                session_id,
                "Cannot read artifacts",
                code="artifact_store",
            )
            return
        builtin_tools = ctx.agent.builtin_tools
        cwd_path, tools = _cwd_and_tools(row.environment, builtin_tools)
        cache_error: ObjectStoreError | None = None
        if settings is not None and cwd_path and ctx.pi_session.present:
            try:
                context_pi = await fetch_pi_session_bytes(
                    ctx.pi_session.model_dump(), settings
                )
            except ObjectStoreError as exc:
                cache_error = exc
            else:
                if context_pi is not None:
                    dest = pi_session_file(Path(cwd_path))
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(context_pi)
        skill_dirs = _skill_dirs(row.environment, builtin_tools)
        env_type = row.environment.get("type")
        network = _network_access(row.environment)
        sandbox_size = sandbox_size_of(row.environment)
        sandbox_mem = (
            mem_mib_for_size(settings, sandbox_size) if settings is not None else None
        )
        stored_image = sandbox_image_of(row.environment)
        sandbox_image = stored_image or image_for_size(sandbox_size)
        extra_env = session_env_from(row.environment)
        spawn_ids = _spawn_identity(row, user_id, org_id)
        await sink.update_session(
            tenant_id,
            session_id,
            changes={
                "status": "in_progress",
                "required_actions": [],
            },
        )
        await sink.append_event(
            hub, tenant_id, session_id, type="agent.session.in_progress"
        )
        turn_id = await sink.create_turn(tenant_id, session_id, status="in_progress")
        await sink.append_event(
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.created",
            data={"turn_id": str(turn_id)},
        )
        await sink.append_event(
            hub,
            tenant_id,
            session_id,
            type="agent.session.turn.in_progress",
            data={"turn_id": str(turn_id)},
        )
        await emit_item(
            hub,
            tenant_id,
            session_id,
            turn_id=turn_id,
            type="message",
            data={"role": "user", "content": item_content},
            sink=sink,
        )
        if cache_error is not None:
            await fail_turn(
                hub,
                tenant_id,
                session_id,
                turn_id,
                "Cannot read artifacts",
                request_id=request_id,
                metrics=metrics,
                tracing=tracing,
                settings=settings,
                code="artifact_store",
                user_id=user_id,
                turn_context=ctx,
                sink=sink,
            )
            return
        composed = compose_instructions(
            settings,
            instructions,
            env_type=env_type if isinstance(env_type, str) else None,
            sandbox_size=sandbox_size,
            mem_mib=sandbox_mem,
            network=network,
            builtin_tools=builtin_tools,
        )
        log.info(
            "turn start",
            extra={
                "session_id": str(session_id),
                "turn_id": str(turn_id),
                "model": model,
                **({"request_id": request_id} if request_id else {}),
            },
        )
        with start_span(
            tracing,
            "turn",
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            model=model,
        ):
            with start_span(
                tracing,
                "model",
                request_id=request_id,
                session_id=session_id,
                turn_id=turn_id,
                model=model,
            ) as model_span:
                generate = harness.generate(
                    text,
                    images=images,
                    session_id=session_id,
                    cwd=cwd_path,
                    tools=tools,
                    function_tools=function_tools,
                    mcp_http=resolved_mcp,
                    skill_dirs=skill_dirs,
                    abort=abort,
                    tenant_id=tenant_id,
                    model=model,
                    instructions=composed,
                    api_key=resolved_key,
                    base_url=resolved_base_url,
                    key_id=key_id,
                    env_type=env_type,
                    mem_mib=sandbox_mem,
                    image=sandbox_image,
                    extra_env=extra_env,
                    turn_id=turn_id,
                    **spawn_ids,
                    **_pi_spawn_overrides(
                        settings, session_metadata, agent_metadata, builtin_tools
                    ),
                    **_idle_spawn_from_context(settings, env_type, ctx),
                )
                retry_state = _new_retry_state()
                try:
                    if turn_timeout is None:
                        reply, pending, usage = await _consume_generate(
                            hub,
                            tenant_id,
                            session_id,
                            turn_id,
                            generate,
                            retry_state,
                            sink=sink,
                        )
                    else:
                        async with asyncio.timeout(turn_timeout.total_seconds()):
                            reply, pending, usage = await _consume_generate(
                                hub,
                                tenant_id,
                                session_id,
                                turn_id,
                                generate,
                                retry_state,
                                sink=sink,
                            )
                except OutboxFull as full:
                    await fail_outbox_full(
                        hub,
                        sink,
                        tenant_id,
                        session_id,
                        turn_id,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        user_id=user_id,
                        reason=full,
                    )
                    return
                except CapacityError as exc:
                    await sink.update_session(
                        tenant_id,
                        session_id,
                        changes={"status": "idle"},
                    )
                    raise ApiError(
                        "invalid_request",
                        str(exc),
                        code=exc.code,
                        status_code=429,
                    ) from exc
                except TurnFailed as exc:
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        exc.message,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code=exc.code,
                        user_id=user_id,
                        failure=exc.failure,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                except TimeoutError:
                    abort.set()
                    await harness.abort(session_id)
                    timed_out = replace(
                        failure_for("turn_timeout", "Turn timed out"),
                        upstream_attempts=_attempts_so_far(retry_state),
                    )
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        timed_out.message,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code="turn_timeout",
                        user_id=user_id,
                        failure=timed_out,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                except OSError as exc:
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        str(exc) or "Cannot start Pi",
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code="spawn_failed",
                        user_id=user_id,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                set_span(
                    tracing,
                    model_span,
                    **_model_span_attrs(
                        request_id=request_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        model=model,
                        usage=usage,
                        status="cancelled" if abort.is_set() else "completed",
                    ),
                )
            if abort.is_set():
                await cancel_turn(
                    hub,
                    tenant_id,
                    session_id,
                    turn_id,
                    request_id=request_id,
                    metrics=metrics,
                    tracing=tracing,
                    settings=settings,
                    user_id=user_id,
                    turn_context=ctx,
                    sink=sink,
                )
                return
            if pending:
                await sink.update_session(
                    tenant_id,
                    session_id,
                    changes={
                        "status": "requires_action",
                        "required_actions": pending,
                    },
                )
                await sink.append_event(
                    hub,
                    tenant_id,
                    session_id,
                    type="agent.session.requires_action",
                    data={"turn_id": str(turn_id), "required_actions": pending},
                )
                return
            await complete_turn(
                hub,
                tenant_id,
                session_id,
                turn_id,
                reply,
                usage,
                request_id=request_id,
                metrics=metrics,
                tracing=tracing,
                settings=settings,
                proc=pool.peek(session_id) if pool is not None else None,
                user_id=user_id,
                turn_context=ctx,
                sink=sink,
            )
    finally:
        if pool is not None:
            pool.release(session_id)
        hub.unwatch_turn(session_id)


async def continue_turn(
    hub: EventBus,
    harness: Harness,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    turn_id: uuid.UUID,
    call_id: str,
    success: bool,
    output: str | None,
    error: str | None,
    mcp_http: list[Any] | None = None,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    turn_timeout: timedelta | None = None,
    settings: Settings | None = None,
    pool: PiPool | None = None,
    api_key: str | None = None,
    key_id: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    turn_context: dict[str, Any] | None = None,
    sink: ResultSink,
) -> None:
    ctx = _require_turn_context(turn_context)
    resolved_key = api_key if api_key is not None else ctx.model.api_key
    resolved_mcp = (
        mcp_http if mcp_http is not None else mcp_servers_from_context(ctx.model_dump())
    )
    resolved_base_url = ctx.model.base_url or None
    row = cast(
        Any,
        SimpleNamespace(
            environment=(
                dict(ctx.session.environment)
                if isinstance(ctx.session.environment, dict)
                else {}
            ),
            metadata_json=dict(ctx.session.metadata),
            key_id=ctx.session.key_id,
            agent_id=uuid_or_none(ctx.session.agent_id),
            user_id=ctx.session.user_id,
            org_id=ctx.session.org_id,
        ),
    )
    if ctx.session.status != "requires_action":
        raise ApiError(
            "invalid_request",
            "Session is not waiting for a tool result",
            code="invalid_request",
        )
    required_actions = ctx.session.required_actions
    actions = [
        action
        for action in (required_actions if isinstance(required_actions, list) else [])
        if isinstance(action, dict)
    ]
    match = next(
        (
            action
            for action in actions
            if action.get("type") == "function_call"
            and action.get("call_id") == call_id
        ),
        None,
    )
    if match is None:
        raise ApiError(
            "invalid_request",
            "Unknown call_id",
            code="invalid_request",
        )
    remaining = [
        action
        for action in actions
        if action.get("type") == "function_call" and action.get("call_id") != call_id
    ]
    if remaining:
        await sink.update_session(
            tenant_id,
            session_id,
            changes={"required_actions": remaining},
        )
        return
    try:
        if pool is not None:
            pool.hold(session_id)
        ensure_openai_workspace(row.environment)
        cwd_path, tools = _cwd_and_tools(row.environment)
        if settings is not None and cwd_path and ctx.pi_session.present:
            try:
                context_pi = await fetch_pi_session_bytes(
                    ctx.pi_session.model_dump(), settings
                )
            except ObjectStoreError:
                await fail_turn(
                    hub,
                    tenant_id,
                    session_id,
                    turn_id,
                    "Cannot read artifacts",
                    request_id=request_id,
                    metrics=metrics,
                    tracing=tracing,
                    settings=settings,
                    code="artifact_store",
                    user_id=user_id,
                    turn_context=ctx,
                    sink=sink,
                )
                return
            if context_pi is not None:
                dest = pi_session_file(Path(cwd_path))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(context_pi)
        function_tools = [dict(item) for item in ctx.agent.function_tools]
        session_metadata = dict(ctx.session.metadata)
        agent_metadata = dict(ctx.agent.metadata)
        try:
            model = _bind_turn_model(settings, ctx.agent.model)
        except ApiError as exc:
            await report_environment_failed(
                sink,
                hub,
                tenant_id,
                session_id,
                exc.message,
                code=exc.code or None,
            )
            return
        builtin_tools = ctx.agent.builtin_tools
        cwd_path, tools = _cwd_and_tools(row.environment, builtin_tools)
        skill_dirs = _skill_dirs(row.environment, builtin_tools)
        env_type = row.environment.get("type")
        network = _network_access(row.environment)
        sandbox_size = sandbox_size_of(row.environment)
        sandbox_mem = (
            mem_mib_for_size(settings, sandbox_size) if settings is not None else None
        )
        stored_image = sandbox_image_of(row.environment)
        sandbox_image = stored_image or image_for_size(sandbox_size)
        extra_env = session_env_from(row.environment)
        spawn_ids = _spawn_identity(row, user_id, org_id)
        result = {
            "call_id": call_id,
            "success": success,
            "output": output,
            "error": error,
        }
        composed = compose_instructions(
            settings,
            ctx.agent.instructions,
            env_type=env_type if isinstance(env_type, str) else None,
            sandbox_size=sandbox_size,
            mem_mib=sandbox_mem,
            network=network,
            builtin_tools=builtin_tools,
        )
        with start_span(
            tracing,
            "turn",
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            model=model,
        ):
            with start_span(
                tracing,
                "model",
                request_id=request_id,
                session_id=session_id,
                turn_id=turn_id,
                model=model,
            ) as model_span:
                generate = harness.generate(
                    "",
                    session_id=session_id,
                    cwd=cwd_path,
                    tools=tools,
                    function_tools=function_tools,
                    tool_result=result,
                    mcp_http=resolved_mcp,
                    skill_dirs=skill_dirs,
                    tenant_id=tenant_id,
                    model=model,
                    instructions=composed,
                    api_key=resolved_key,
                    base_url=resolved_base_url,
                    key_id=key_id,
                    env_type=env_type,
                    mem_mib=sandbox_mem,
                    image=sandbox_image,
                    extra_env=extra_env,
                    turn_id=turn_id,
                    **spawn_ids,
                    **_pi_spawn_overrides(
                        settings, session_metadata, agent_metadata, builtin_tools
                    ),
                    **_idle_spawn_from_context(settings, env_type, ctx),
                )
                retry_state = _new_retry_state()
                try:
                    if turn_timeout is None:
                        reply, pending, usage = await _consume_generate(
                            hub,
                            tenant_id,
                            session_id,
                            turn_id,
                            generate,
                            retry_state,
                            sink=sink,
                        )
                    else:
                        async with asyncio.timeout(turn_timeout.total_seconds()):
                            reply, pending, usage = await _consume_generate(
                                hub,
                                tenant_id,
                                session_id,
                                turn_id,
                                generate,
                                retry_state,
                                sink=sink,
                            )
                except OutboxFull as full:
                    await fail_outbox_full(
                        hub,
                        sink,
                        tenant_id,
                        session_id,
                        turn_id,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        user_id=user_id,
                        reason=full,
                    )
                    return
                except TurnFailed as exc:
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        exc.message,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code=exc.code,
                        user_id=user_id,
                        failure=exc.failure,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                except TimeoutError:
                    await harness.abort(session_id)
                    timed_out = replace(
                        failure_for("turn_timeout", "Turn timed out"),
                        upstream_attempts=_attempts_so_far(retry_state),
                    )
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        timed_out.message,
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code="turn_timeout",
                        user_id=user_id,
                        failure=timed_out,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                except OSError as exc:
                    await fail_turn(
                        hub,
                        tenant_id,
                        session_id,
                        turn_id,
                        str(exc) or "Cannot start Pi",
                        request_id=request_id,
                        metrics=metrics,
                        tracing=tracing,
                        settings=settings,
                        code="spawn_failed",
                        user_id=user_id,
                        turn_context=ctx,
                        sink=sink,
                    )
                    return
                except CapacityError as exc:
                    raise ApiError(
                        "invalid_request",
                        str(exc),
                        code=exc.code,
                        status_code=429,
                    ) from exc
                set_span(
                    tracing,
                    model_span,
                    **_model_span_attrs(
                        request_id=request_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        model=model,
                        usage=usage,
                        status="completed",
                    ),
                )
            if pending:
                await sink.update_session(
                    tenant_id,
                    session_id,
                    changes={
                        "status": "requires_action",
                        "required_actions": pending,
                    },
                )
                await sink.append_event(
                    hub,
                    tenant_id,
                    session_id,
                    type="agent.session.requires_action",
                    data={"turn_id": str(turn_id), "required_actions": pending},
                )
                return
            await complete_turn(
                hub,
                tenant_id,
                session_id,
                turn_id,
                reply,
                usage,
                request_id=request_id,
                metrics=metrics,
                tracing=tracing,
                settings=settings,
                proc=pool.peek(session_id) if pool is not None else None,
                user_id=user_id,
                turn_context=ctx,
                sink=sink,
            )
    finally:
        if pool is not None:
            pool.release(session_id)
