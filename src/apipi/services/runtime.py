import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import CapacityError, Settings
from apipi.env.setup import SetupError, provision_hosted_async, session_env_from
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics, observe_turn
from apipi.gateway.otel import Tracing, set_span, start_span
from apipi.services.agents import definition_for_session
from apipi.services.event_bus import EventBus, InMemoryEventBus
from apipi.services.failures import (
    Failure,
    cancel_data,
    error_mode,
    failure_for,
    failure_from_payload,
    log_extra,
    log_level_for,
    session_error_data,
    turn_failed_data,
    usage_fields,
)
from apipi.services.payload_export import export_payload
from apipi.services.sink import (
    LIVE_EVENT_TYPES,
    ResultSink,
    live_event_body,
)
from apipi.services.sink import (
    PUBLIC_EVENT_TYPES as PUBLIC_EVENT_TYPES,
)
from apipi.services.sink import (
    event_body as event_body,
)
from apipi.services.sink import (
    persist_event as persist_event,
)
from apipi.services.skills import discover_skill_dirs, unpack_skill_zip
from apipi.services.turn_context import (
    fetch_pi_session_bytes,
    materialize_skill_zips,
    materialize_workspace_files,
    mcp_servers_from_context,
)
from apipi.services.usage import add_usage, empty_usage, usage_event, usage_from
from apipi.services.usage_export import export_usage
from apipi.store.blobs import ObjectStoreError
from apipi.store.events import list_events
from apipi.store.models import SessionRow, utc_now
from apipi.store.repo import (
    add_usage_rollup,
    append_turn_log,
    get_session,
    get_session_turn,
    list_items,
    list_turns,
    update_session,
)
from apipi.worker.outbox import OutboxFull
from apipi.worker.pi.artifacts import (
    ensure_openai_workspace,
)
from apipi.worker.pi.dirs import pi_session_file
from apipi.worker.pi.model_host import note_pi_model, require_model
from apipi.worker.pi.platform_prompt import compose_instructions
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc
from apipi.worker.pi.sandbox import (
    image_for_size,
    mem_mib_for_size,
    sandbox_image_of,
    sandbox_size_of,
)
from apipi.worker.pi.settings_json import (
    resolve_builtin_tools,
    resolve_codemode,
    resolve_system_prompt,
    resolve_thinking,
)
from apipi.worker.turn_context import (
    ContextBytes,
    TurnContext,
    parse_turn_context,
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


FAKE_USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 7,
    "cache_read_tokens": 3,
    "cache_write_tokens": 2,
    "total_tokens": 23,
}


class FakeHarness:
    def __init__(self) -> None:
        self.function_calls: list[dict[str, Any]] = []
        self.mcp_calls: list[dict[str, Any]] = []
        self.function_tools: list[dict[str, Any]] | None = None
        self.mcp_http: list[Any] | None = None
        self.skill_dirs: list[str] | None = None
        self.instructions: str | None = None
        self.tools: bool | None = None
        self.hold = False
        self.fail_message: str | None = None
        self.usage: dict[str, int] = dict(FAKE_USAGE)

    def complete(self, text: str) -> str:
        return text if text else "ok"

    async def abort(self, session_id: uuid.UUID) -> None:
        del session_id

    async def generate(
        self,
        text: str,
        *,
        session_id: uuid.UUID | None = None,
        cwd: str | None = None,
        tools: bool = True,
        function_tools: list[dict[str, Any]] | None = None,
        tool_result: dict[str, Any] | None = None,
        mcp_http: list[Any] | None = None,
        skill_dirs: list[str] | None = None,
        abort: asyncio.Event | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        del session_id, cwd
        self.tools = tools
        if self.hold:
            if abort is not None:
                await abort.wait()
            return
        if self.fail_message is not None:
            yield ("pi_error", {"message": self.fail_message})
            return
        self.function_tools = (
            list(function_tools) if function_tools is not None else None
        )
        self.mcp_http = list(mcp_http) if mcp_http is not None else None
        self.skill_dirs = list(skill_dirs) if skill_dirs is not None else None
        raw_instructions = _kwargs.get("instructions")
        self.instructions = (
            raw_instructions
            if isinstance(raw_instructions, str) and raw_instructions
            else None
        )
        if tool_result is not None:
            if tool_result.get("success"):
                output = tool_result.get("output")
                reply = output if isinstance(output, str) and output else "ok"
            else:
                error = tool_result.get("error")
                reply = error if isinstance(error, str) and error else "error"
            yield ("agent.session.turn.output_text.delta", {"delta": reply})
            yield ("agent.session.turn.output_text.done", {"text": reply})
            yield ("usage", usage_from(self.usage))
            return
        if self.function_calls:
            call = self.function_calls.pop(0)
            yield ("function_call", dict(call))
            return
        for call in self.mcp_calls:
            yield (
                "agent.session.turn.item.added",
                {
                    "item_type": "mcp_call",
                    "call_id": call.get("call_id"),
                    "name": call.get("name"),
                },
            )
        self.mcp_calls = []
        reply = self.complete(text)
        yield ("agent.session.turn.output_text.delta", {"delta": reply})
        yield ("agent.session.turn.output_text.done", {"text": reply})
        yield ("usage", usage_from(self.usage))


def _function_tools(tools: list[Any] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    return [
        tool
        for tool in tools
        if isinstance(tool, dict) and tool.get("type") == "function"
    ]


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


def _effective_builtin_tools(
    environment: dict[str, Any] | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    if isinstance(environment, dict) and environment.get("type") == "none":
        return "off"
    return resolve_builtin_tools(session_metadata, agent_metadata)


def _effective_codemode(
    builtin_tools: str,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
) -> str:
    if builtin_tools == "off":
        return "off"
    return resolve_codemode(session_metadata, agent_metadata)


def _uuid_or_none(raw: str | None) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


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
        "codemode": _effective_codemode(
            builtin_tools, session_metadata, agent_metadata
        ),
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


async def _emit_item(
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    turn_id: uuid.UUID,
    type: str,
    data: dict[str, Any],
    sink: ResultSink,
) -> None:
    item_id = await sink.create_item(
        tenant_id, session_id, type=type, turn_id=turn_id, data=data
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.added",
        data={"item_id": str(item_id), "item_type": type, "turn_id": str(turn_id)},
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.done",
        data={"item_id": str(item_id), "turn_id": str(turn_id)},
    )


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
            await _emit_item(
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
            sink.tally_mcp(_mcp_name(payload))
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


def _tally(names: list[str]) -> tuple[list[str], dict[str, int]]:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return list(counts), counts


def _latency_ms(started: datetime) -> int:
    begin = started if started.tzinfo is not None else started.replace(tzinfo=UTC)
    ms = int((utc_now() - begin).total_seconds() * 1000)
    return max(ms, 0)


def _mcp_labels(tools: list[Any] | None) -> list[str]:
    labels: list[str] = []
    if not tools:
        return labels
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        label = tool.get("server_label")
        if isinstance(label, str) and label:
            labels.append(label)
    return labels


def _mcp_name(data: dict[str, Any]) -> str | None:
    label = data.get("server_label")
    if isinstance(label, str) and label:
        return label
    name = data.get("name")
    if isinstance(name, str) and name:
        return name
    return None


async def _tool_mcp_for_turn(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    labels: list[str],
) -> tuple[list[str], dict[str, int], list[str], dict[str, int]]:
    del labels
    tool_names: list[str] = []
    mcp_names: list[str] = []
    items = await list_items(db, tenant_id, session_id)
    if items is not None:
        for item in items:
            if item.turn_id != turn_id or item.type != "function_call":
                continue
            name = item.data.get("name")
            if isinstance(name, str) and name:
                tool_names.append(name)
    events = await list_events(db, tenant_id, session_id)
    for event in events:
        if event.type not in (
            "agent.session.turn.item.added",
            "agent.session.turn.item.nested",
        ):
            continue
        if event.data.get("turn_id") != str(turn_id):
            continue
        if event.data.get("item_type") != "mcp_call":
            continue
        name = _mcp_name(event.data)
        if name is not None:
            mcp_names.append(name)
    tools, tool_counts = _tally(tool_names)
    mcps, mcp_counts = _tally(mcp_names)
    return tools, tool_counts, mcps, mcp_counts


async def _write_turn_log(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    status: str,
    usage: dict[str, int] | None = None,
    error_code: str | None = None,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    settings: Settings | None = None,
    artifact_bytes: int = 0,
    user_id: str | None = None,
    failure: Failure | None = None,
    turn_context: TurnContext | None = None,
    tool_names: list[str] | None = None,
    tool_counts: dict[str, int] | None = None,
    mcp_names: list[str] | None = None,
    mcp_counts: dict[str, int] | None = None,
    run_mode: str = "",
) -> None:
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is None:
        return
    if turn_context is not None:
        agent_id = _uuid_or_none(turn_context.session.agent_id)
        key_id = turn_context.session.key_id
        raw_type = turn_context.session.environment.get("type")
        environment_type = raw_type if isinstance(raw_type, str) else ""
        model = turn_context.agent.model
        labels = [server.server_label for server in turn_context.mcp]
    else:
        row = await get_session(db, tenant_id, session_id)
        agent_id = row.agent_id if row is not None else None
        key_id = row.key_id if row is not None else ""
        environment_type = ""
        if row is not None and isinstance(row.environment, dict):
            raw_type = row.environment.get("type")
            if isinstance(raw_type, str):
                environment_type = raw_type
        model = None
        labels = []
        if row is not None and row.agent_id is not None:
            definition = await definition_for_session(db, tenant_id, row)
            if isinstance(definition, dict):
                raw_model = definition.get("model")
                model = raw_model if isinstance(raw_model, str) else None
                raw_tools = definition.get("tools")
                labels = _mcp_labels(raw_tools if isinstance(raw_tools, list) else [])
    stored = usage_from(usage)
    if (
        tool_names is None
        or tool_counts is None
        or mcp_names is None
        or mcp_counts is None
    ):
        # Stale recovery (a turn this process never observed): fall back
        # to the stored rows so the turn log keeps its tool summary.
        tool_names, tool_counts, mcp_names, mcp_counts = await _tool_mcp_for_turn(
            db, tenant_id, session_id, turn_id, labels
        )
    latency_ms = _latency_ms(turn.created_at)
    instance_id = settings.instance_id if settings is not None else None
    store = settings.usage_store if settings is not None else "turns"
    created = utc_now()
    event = usage_event(
        tenant_id=tenant_id,
        key_id=key_id,
        session_id=session_id,
        turn_id=turn_id,
        agent_id=agent_id,
        model=model,
        status=status,
        latency_ms=latency_ms,
        usage=stored,
        tool_names=tool_names,
        tool_counts=tool_counts,
        mcp_names=mcp_names,
        mcp_counts=mcp_counts,
        environment_type=environment_type,
        run_mode=run_mode,
        instance_id=instance_id,
        artifact_bytes=artifact_bytes,
        request_id=request_id,
        error_code=error_code,
        created_at=created,
        user_id=user_id,
        **usage_fields(failure),
    )
    if store == "turns":
        await append_turn_log(
            db,
            tenant_id,
            session_id,
            turn_id,
            status=status,
            agent_id=agent_id,
            model=model,
            latency_ms=latency_ms,
            prompt_tokens=stored["prompt_tokens"],
            completion_tokens=stored["completion_tokens"],
            cache_read_tokens=stored["cache_read_tokens"],
            cache_write_tokens=stored["cache_write_tokens"],
            total_tokens=stored["total_tokens"],
            error_code=error_code,
            failure_source=failure.failure_source if failure is not None else None,
            upstream_status=failure.upstream_status if failure is not None else None,
            retryable=failure.retryable if failure is not None else None,
            legacy_code=failure.legacy_code if failure is not None else None,
            upstream_attempts=(
                failure.upstream_attempts if failure is not None else None
            ),
            request_id=request_id,
            tool_names=tool_names,
            tool_counts=tool_counts,
            mcp_names=mcp_names,
            mcp_counts=mcp_counts,
            key_id=key_id,
            environment_type=environment_type,
            run_mode=run_mode,
            instance_id=instance_id,
            artifact_bytes=artifact_bytes,
        )
    if store in {"turns", "rollups"}:
        await add_usage_rollup(
            db,
            tenant_id,
            created.date(),
            prompt_tokens=stored["prompt_tokens"],
            completion_tokens=stored["completion_tokens"],
            cache_read_tokens=stored["cache_read_tokens"],
            cache_write_tokens=stored["cache_write_tokens"],
            total_tokens=stored["total_tokens"],
            turns=1,
            artifact_bytes=artifact_bytes,
        )
    if status == "failed" and failure is not None:
        log_event(
            log,
            log_level_for(failure),
            "turn failed",
            event="turn.failed",
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            status=status,
            latency_ms=latency_ms,
            **log_extra(failure),
        )
    elif status == "failed":
        log_event(
            log,
            logging.ERROR,
            "turn failed",
            event="turn.failed",
            error_code=error_code,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            status=status,
            latency_ms=latency_ms,
        )
    else:
        extra = log_extra(failure) if failure is not None else {}
        log_event(
            log,
            logging.INFO,
            "turn",
            event="turn",
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            status=status,
            latency_ms=latency_ms,
            **extra,
        )
    observe_turn(
        metrics,
        tenant_id=tenant_id,
        status=status,
        latency_ms=latency_ms,
        prompt_tokens=stored["prompt_tokens"],
        completion_tokens=stored["completion_tokens"],
        cache_read_tokens=stored["cache_read_tokens"],
        cache_write_tokens=stored["cache_write_tokens"],
        total_tokens=stored["total_tokens"],
        error_code=error_code,
    )
    set_span(
        tracing,
        request_id=request_id,
        session_id=session_id,
        turn_id=turn_id,
        model=model,
        status=status,
        prompt_tokens=stored["prompt_tokens"],
        completion_tokens=stored["completion_tokens"],
        cache_read_tokens=stored["cache_read_tokens"],
        cache_write_tokens=stored["cache_write_tokens"],
        total_tokens=stored["total_tokens"],
        tool_names=tool_names,
    )
    try:
        export_usage(settings, metrics, event)
    except Exception:
        log_event(
            log,
            logging.WARNING,
            "usage export failed",
            event="usage.export.dropped",
            error_code="export_drop",
            exc_info=True,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
        )
    try:
        items = await list_items(db, tenant_id, session_id)
        export_payload(
            settings,
            metrics,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
            items=items or [],
        )
    except Exception:
        log_event(
            log,
            logging.WARNING,
            "payload export failed",
            event="payload.export.dropped",
            error_code="export_drop",
            exc_info=True,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
        )


async def _harvest_split(
    sink: ResultSink,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    proc: Any | None,
    *,
    settings: Any,
    turn_context: Any | None = None,
    request_id: str | None = None,
    metrics: Any | None = None,
    tracing: Any | None = None,
    user_id: str | None = None,
) -> int | None:
    """Harvest the turn's files through presigned uploads.

    Returns artifact bytes for the turn log, or None when the turn
    already failed (quota `artifact_store`) and the caller must stop.
    """
    from apipi.config import DiskLimitError
    from apipi.store.blobs import ObjectStoreError
    from apipi.worker.pi.artifacts import (
        _hosted_files,
        read_pi_session_bytes,
    )

    files: list[tuple[str, bytes]] = []
    workspace_error: DiskLimitError | None = None
    dest: Any | None = None
    try:
        env: dict[str, Any] | None = None
        if turn_context is not None:
            session_obj = getattr(turn_context, "session", None)
            raw_env = getattr(session_obj, "environment", None)
            if isinstance(raw_env, dict):
                env = raw_env
        if env is not None and env.get("type") == "openai_hosted":
            directory = env.get("directory")
            if isinstance(directory, str) and directory:
                from pathlib import Path as _Path

                dest = _Path(directory)
    except Exception:
        dest = None
    try:
        hosted, workspace_error = await _hosted_files(
            proc,
            dest,
            sync_workspace=False,
            max_workspace_bytes=settings.max_workspace_bytes,
        )
        files = hosted
        if not files and dest is not None:
            from apipi.worker.pi.artifacts import read_workspace_artifacts as _read_ws

            try:
                files = _read_ws(dest)
            except Exception:
                files = []
    except DiskLimitError as exc:
        workspace_error = exc
    except (OSError, Exception):
        files = []
    limit_error: DiskLimitError | None = workspace_error
    if files:
        try:
            await sink.store_artifacts(
                tenant_id,
                session_id,
                files,
                turn_id=turn_id,
                settings=settings,
            )
        except DiskLimitError as exc:
            limit_error = exc
        except (OSError, ObjectStoreError, Exception):
            limit_error = DiskLimitError(
                "Cannot write artifacts", code="artifact_store"
            )
    try:
        pi_data = await read_pi_session_bytes(proc, dest)
    except Exception:
        pi_data = b""
    if pi_data:
        try:
            await sink.store_pi_session(
                tenant_id, session_id, pi_data, settings=settings
            )
        except (OSError, ObjectStoreError, Exception) as exc:
            if isinstance(exc, DiskLimitError):
                limit_error = limit_error or exc
            else:
                limit_error = limit_error or DiskLimitError(
                    "Cannot write artifacts", code="artifact_store"
                )
    if limit_error is not None:
        if limit_error.code == "artifact_store":
            await _fail_turn(
                hub,
                tenant_id,
                session_id,
                turn_id,
                str(limit_error),
                request_id=request_id,
                metrics=metrics,
                tracing=tracing,
                settings=settings,
                code="artifact_store",
                user_id=user_id,
                turn_context=turn_context,
                sink=sink,
            )
            return None
        await sink.append_event(
            hub,
            tenant_id,
            session_id,
            type="agent.session.error",
            data={"message": str(limit_error), "code": limit_error.code},
        )
    return sum(len(data) for _rel, data in files)


async def _complete_turn(
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    reply: str,
    usage: dict[str, int],
    *,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    settings: Settings | None = None,
    proc: PiProc | None = None,
    user_id: str | None = None,
    turn_context: TurnContext | None = None,
    sink: ResultSink,
) -> None:
    await _emit_item(
        hub,
        tenant_id,
        session_id,
        turn_id=turn_id,
        type="message",
        data={"role": "assistant", "content": reply},
        sink=sink,
    )
    stored = usage_from(usage)
    await sink.finish_turn(
        tenant_id, session_id, turn_id, status="completed", usage=stored
    )
    published = 0
    if settings is not None:
        harvested = await _harvest_split(
            sink,
            hub,
            tenant_id,
            session_id,
            turn_id,
            proc,
            settings=settings,
            turn_context=turn_context,
            request_id=request_id,
            metrics=metrics,
            tracing=tracing,
            user_id=user_id,
        )
        if harvested is None:
            return
        published = harvested
    await sink.write_turn_log(
        hub,
        tenant_id,
        session_id,
        turn_id,
        status="completed",
        usage=stored,
        request_id=request_id,
        metrics=metrics,
        tracing=tracing,
        settings=settings,
        artifact_bytes=published,
        user_id=user_id,
        turn_context=turn_context,
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.completed",
        data={"turn_id": str(turn_id), "usage": stored},
    )
    await sink.update_session(
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await sink.append_event(hub, tenant_id, session_id, type="agent.session.idle")


async def _cancel_turn(
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    settings: Settings | None = None,
    user_id: str | None = None,
    turn_context: TurnContext | None = None,
    sink: ResultSink,
) -> None:
    cancelled = failure_for("cancelled", "Cancelled")
    await sink.finish_turn(
        tenant_id, session_id, turn_id, status="cancelled", failure=cancelled
    )
    await sink.write_turn_log(
        hub,
        tenant_id,
        session_id,
        turn_id,
        status="cancelled",
        request_id=request_id,
        metrics=metrics,
        tracing=tracing,
        settings=settings,
        user_id=user_id,
        error_code=cancelled.code,
        failure=cancelled,
        turn_context=turn_context,
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.cancelled",
        data=cancel_data(str(turn_id)),
    )
    await sink.update_session(
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await sink.append_event(hub, tenant_id, session_id, type="agent.session.idle")


def lease_live(until: datetime | None) -> bool:
    if until is None:
        return False
    current = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    return current > utc_now()


async def fail_stale_in_progress(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    message: str = "Turn interrupted",
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id)
    if row is None or row.status != "in_progress":
        return row
    if lease_live(row.lease_until):
        return row
    turns = await list_turns(db, tenant_id, session_id) or []
    for turn in reversed(turns):
        if turn.status == "in_progress":
            await _fail_turn_in_db(
                db,
                hub,
                tenant_id,
                session_id,
                turn.id,
                message,
                code="turn_interrupted",
            )
            return await get_session(db, tenant_id, session_id)
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")
    return await get_session(db, tenant_id, session_id)


async def prepare_for_new_turn(
    hub: EventBus,
    harness: Harness,
    session_id: uuid.UUID,
) -> None:
    abort = hub.turn_abort(session_id)
    if abort is not None:
        abort.set()
        await harness.abort(session_id)


async def _fail_outbox_full(
    hub: EventBus,
    sink: ResultSink,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    *,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    settings: Settings | None = None,
    user_id: str | None = None,
) -> None:
    """Fail a turn whose outbox is full, spending the emergency budget."""
    from apipi.worker.outbox import OutboxFull

    abort = hub.turn_abort(session_id)
    if abort is not None:
        abort.set()
    try:
        async with sink.emergency_mode():
            await _fail_turn(
                hub,
                tenant_id,
                session_id,
                turn_id,
                "Worker outbox is full",
                request_id=request_id,
                metrics=metrics,
                tracing=tracing,
                settings=settings,
                code="worker_outbox_full",
                user_id=user_id,
                sink=sink,
            )
    except OutboxFull:
        log.error(
            "worker outbox full, turn failure dropped",
            extra={
                "session_id": str(session_id),
                "turn_id": str(turn_id),
                "event": "worker.outbox.dropped",
                "error_code": "worker_outbox_full",
            },
        )


async def _fail_turn(
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    message: str,
    *,
    request_id: str | None = None,
    metrics: Metrics | None = None,
    tracing: Tracing | None = None,
    settings: Settings | None = None,
    code: str = "model_host_error",
    user_id: str | None = None,
    failure: Failure | None = None,
    turn_context: TurnContext | None = None,
    sink: ResultSink,
) -> None:
    resolved = failure or failure_for(code, message)
    await sink.finish_turn(
        tenant_id, session_id, turn_id, status="failed", failure=resolved
    )
    await sink.write_turn_log(
        hub,
        tenant_id,
        session_id,
        turn_id,
        status="failed",
        request_id=request_id,
        metrics=metrics,
        tracing=tracing,
        settings=settings,
        user_id=user_id,
        error_code=resolved.code,
        failure=resolved,
        turn_context=turn_context,
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.failed",
        data=turn_failed_data(str(turn_id), resolved),
    )
    await sink.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_error_data(resolved, mode=error_mode(settings)),
    )
    await sink.update_session(
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await sink.append_event(hub, tenant_id, session_id, type="agent.session.idle")


async def _fail_turn_in_db(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    message: str,
    *,
    code: str,
) -> None:
    resolved = failure_for(code, message)
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "failed"
        turn.updated_at = utc_now()
        await db.flush()
    await _write_turn_log(
        db,
        tenant_id,
        session_id,
        turn_id,
        status="failed",
        error_code=resolved.code,
        failure=resolved,
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.failed",
        data=turn_failed_data(str(turn_id), resolved),
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_error_data(resolved, mode=error_mode(None)),
    )
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


def request_cancel(
    hub: EventBus, session_id: uuid.UUID, *, status: str
) -> asyncio.Event | None:
    abort = hub.turn_abort(session_id)
    if status != "in_progress" and abort is None:
        raise ApiError(
            "invalid_request",
            "Session is not in_progress",
            code="invalid_request",
        )
    return abort


def _bind_turn_model(settings: Settings | None, model: str | None) -> str:
    resolved = require_model(model)
    if settings is not None and settings.model_base_url:
        note_pi_model(settings, resolved)
    return resolved


def _session_failed_data(message: str, code: str | None) -> dict[str, Any]:
    if code:
        return session_error_data(failure_for(code, message), mode="legacy")
    return {"message": message}


async def fail_session(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "failed", "required_actions": []},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=_session_failed_data(message, code),
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.failed")


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
        data=_session_failed_data(message, code),
    )
    await sink.append_event(hub, tenant_id, session_id, type="agent.session.failed")


async def fail_environment(
    db: AsyncSession,
    hub: EventBus,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
    *,
    code: str | None = None,
) -> None:
    from apipi.services.sandbox_status import note_failed

    data = await note_failed(db, hub, tenant_id, session_id, message, code=code)
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.environment.failed",
        data=data,
    )
    await fail_session(db, hub, tenant_id, session_id, message, code=code)


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
    from apipi.worker.pi.platform_prompt import compose_instructions
    from apipi.worker.pi.sandbox import (
        image_for_size,
        mem_mib_for_size,
        sandbox_image_of,
        sandbox_size_of,
    )

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
            agent_id=_uuid_or_none(ctx.session.agent_id),
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
        [ref.model_dump() for ref in ctx.files], settings
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
    images: list[dict[str, str]] | None = None,
    parts: list[dict[str, str]] | None = None,
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
        item_content: str | list[dict[str, Any]] = text
        ordered = parts or []
        has_image = any(part.get("type") == "image" for part in ordered)
        if has_image and settings is not None:
            stored_parts: list[dict[str, Any]] = []
            for part in ordered:
                if part.get("type") == "input_text":
                    stored_parts.append(
                        {"type": "input_text", "text": str(part.get("text") or "")}
                    )
                    continue
                if part.get("type") != "image":
                    continue
                mime = str(part.get("mimeType") or "application/octet-stream")
                data = base64.b64decode(str(part.get("data") or ""))
                created = await sink.store_input_image(
                    tenant_id,
                    session_id,
                    data=data,
                    filename="image",
                    content_type=mime,
                    settings=settings,
                )
                file_id = created.get("file_id") or created.get("id")
                stored_parts.append({"type": "input_image", "file_id": file_id})
            if len(stored_parts) == 1 and stored_parts[0].get("type") == "input_text":
                item_content = str(stored_parts[0].get("text") or text)
            elif stored_parts:
                item_content = stored_parts
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
                agent_id=_uuid_or_none(ctx.session.agent_id),
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
                    [ref.model_dump() for ref in ctx.files], settings
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
        await _emit_item(
            hub,
            tenant_id,
            session_id,
            turn_id=turn_id,
            type="message",
            data={"role": "user", "content": item_content},
            sink=sink,
        )
        if cache_error is not None:
            await _fail_turn(
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
                except OutboxFull:
                    await _fail_outbox_full(
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
                    await _fail_turn(
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
                    await _fail_turn(
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
                    await _fail_turn(
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
                await _cancel_turn(
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
            await _complete_turn(
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
            agent_id=_uuid_or_none(ctx.session.agent_id),
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
                await _fail_turn(
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
                except OutboxFull:
                    await _fail_outbox_full(
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
                    )
                    return
                except TurnFailed as exc:
                    await _fail_turn(
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
                    await _fail_turn(
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
                    await _fail_turn(
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
            await _complete_turn(
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
