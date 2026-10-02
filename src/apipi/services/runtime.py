import asyncio
import base64
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import CapacityError, Settings
from apipi.env.setup import SetupError, provision_hosted_async, session_env_from
from apipi.gateway.errors import ApiError
from apipi.gateway.logutil import log_event
from apipi.gateway.metrics import Metrics, observe_turn
from apipi.gateway.otel import Tracing, set_span, start_span
from apipi.services.agents import definition_for_session
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
from apipi.services.files import FileService
from apipi.services.payload_export import export_payload
from apipi.services.skill_store import SkillService
from apipi.services.skills import discover_skill_dirs
from apipi.services.usage import add_usage, empty_usage, usage_event, usage_from
from apipi.services.usage_export import export_usage
from apipi.store.blobs import ArtifactBlobs, ObjectStore, ObjectStoreError, object_store
from apipi.store.engine import Store
from apipi.store.events import append_event, list_events
from apipi.store.models import Event, SessionRow, utc_now
from apipi.store.repo import (
    add_usage_rollup,
    append_turn_log,
    artifact_bytes_for_turn,
    create_item,
    create_turn,
    get_session,
    get_session_turn,
    list_items,
    list_turns,
    update_session,
)
from apipi.worker.pi.artifacts import (
    ensure_openai_workspace,
    harvest_session,
    restore_pi_session,
)
from apipi.worker.pi.idle import resolve_idle_ttl
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


PUBLIC_EVENT_TYPES = frozenset(
    {
        "agent.session.created",
        "agent.session.in_progress",
        "agent.session.idle",
        "agent.session.requires_action",
        "agent.session.failed",
        "agent.session.error",
        "agent.session.turn.created",
        "agent.session.turn.in_progress",
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
        "agent.session.turn.output_text.delta",
        "agent.session.turn.output_text.done",
        "agent.session.turn.item.added",
        "agent.session.turn.item.done",
        "agent.session.turn.item.nested",
        "agent.session.turn.thinking.started",
        "agent.session.turn.thinking.completed",
        "agent.session.turn.compaction.started",
        "agent.session.turn.compaction.completed",
        "agent.session.turn.retrying",
        "agent.session.turn.retry.completed",
        "agent.session.environment.pending",
        "agent.session.environment.connected",
        "agent.session.environment.disconnected",
        "agent.session.environment.failed",
    }
)

LIVE_EVENT_TYPES = frozenset({"agent.session.turn.output_text.delta"})


def event_body(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "type": event.type,
        "seq": event.seq,
        "session_id": str(event.session_id),
        "created_at": event.created_at.isoformat(),
        "data": event.data,
    }


def live_event_body(
    session_id: uuid.UUID, *, type: str, data: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "type": type,
        "session_id": str(session_id),
        "data": data if data is not None else {},
    }


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, list[asyncio.Queue[dict[str, Any]]]] = {}
        self._abort: dict[uuid.UUID, asyncio.Event] = {}

    def watch_turn(self, session_id: uuid.UUID) -> asyncio.Event:
        ev = asyncio.Event()
        self._abort[session_id] = ev
        return ev

    def turn_abort(self, session_id: uuid.UUID) -> asyncio.Event | None:
        return self._abort.get(session_id)

    def unwatch_turn(self, session_id: uuid.UUID) -> None:
        self._abort.pop(session_id, None)

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(
        self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        subs = self._subs.get(session_id)
        if subs is None:
            return
        if queue in subs:
            subs.remove(queue)
        if not subs:
            del self._subs[session_id]

    def publish(self, session_id: uuid.UUID, event: dict[str, Any]) -> None:
        for queue in list(self._subs.get(session_id, ())):
            queue.put_nowait(event)


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


async def persist_event(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any] | None = None,
) -> Event | None:
    if type not in PUBLIC_EVENT_TYPES:
        return None
    if type in LIVE_EVENT_TYPES:
        hub.publish(session_id, live_event_body(session_id, type=type, data=data))
        return None
    event = await append_event(db, tenant_id, session_id, type=type, data=data)
    hub.publish(session_id, event_body(event))
    return event


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


def _idle_spawn(
    settings: Settings | None,
    env_type: str | None,
    *,
    session_idle: str | None,
    session_metadata: dict[str, Any] | None,
    agent_idle: str | None,
) -> dict[str, Any]:
    if settings is None:
        return {}
    return {
        "idle_ttl": resolve_idle_ttl(
            settings,
            env_type,
            session_idle=session_idle,
            session_metadata=session_metadata,
            agent_idle=agent_idle,
        ),
        "idle_ttl_set": True,
    }


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


async def _agent_tools_and_model(
    db: AsyncSession, tenant_id: uuid.UUID, row: SessionRow
) -> tuple[
    list[dict[str, Any]],
    str | None,
    str | None,
    list[Any],
    dict[str, Any],
    str | None,
]:
    if row.agent_id is None:
        return [], row.model, row.instructions, [], {}, None
    definition = await definition_for_session(db, tenant_id, row)
    if definition is None:
        return [], row.model, row.instructions, [], {}, None
    raw_tools = definition.get("tools")
    raw: list[Any] = raw_tools if isinstance(raw_tools, list) else []
    meta = definition.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    model = definition.get("model")
    instructions = definition.get("instructions")
    idle = definition.get("idle_ttl")
    return (
        _function_tools(raw),
        model if isinstance(model, str) else None,
        instructions if isinstance(instructions, str) else None,
        raw,
        meta,
        idle if isinstance(idle, str) else None,
    )


async def _emit_item(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    turn_id: uuid.UUID,
    type: str,
    data: dict[str, Any],
) -> None:
    item = await create_item(
        db, tenant_id, session_id, type=type, turn_id=turn_id, data=data
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.added",
        data={"item_id": str(item.id), "item_type": type, "turn_id": str(turn_id)},
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.item.done",
        data={"item_id": str(item.id), "turn_id": str(turn_id)},
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
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    events: AsyncIterator[tuple[str, dict[str, Any]]],
    retry_state: dict[str, Any] | None = None,
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
            hub.publish(
                session_id, live_event_body(session_id, type=etype, data=payload)
            )
            continue
        async with store.session() as db:
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
                await _emit_item(
                    db,
                    hub,
                    tenant_id,
                    session_id,
                    turn_id=turn_id,
                    type="function_call",
                    data=call,
                )
                continue
            if etype == "agent.session.turn.output_text.done":
                text_out = payload.get("text")
                if isinstance(text_out, str):
                    reply = text_out
            await persist_event(
                db, hub, tenant_id, session_id, type=etype, data=payload
            )
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
) -> None:
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is None:
        return
    row = await get_session(db, tenant_id, session_id)
    agent_id = row.agent_id if row is not None else None
    key_id = row.key_id if row is not None else ""
    environment_type = ""
    if row is not None and isinstance(row.environment, dict):
        raw_type = row.environment.get("type")
        if isinstance(raw_type, str):
            environment_type = raw_type
    model: str | None = None
    labels: list[str] = []
    if row is not None and row.agent_id is not None:
        definition = await definition_for_session(db, tenant_id, row)
        if isinstance(definition, dict):
            raw_model = definition.get("model")
            model = raw_model if isinstance(raw_model, str) else None
            raw_tools = definition.get("tools")
            labels = _mcp_labels(raw_tools if isinstance(raw_tools, list) else [])
    stored = usage_from(usage)
    tool_names, tool_counts, mcp_names, mcp_counts = await _tool_mcp_for_turn(
        db, tenant_id, session_id, turn_id, labels
    )
    latency_ms = _latency_ms(turn.created_at)
    if settings is not None:
        from apipi.worker.pi.isolation import isolation_name

        run_mode = isolation_name(settings.run_mode)
    else:
        run_mode = ""
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


async def _complete_turn(
    db: AsyncSession,
    hub: EventHub,
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
    blobs: ArtifactBlobs | None = None,
) -> None:
    await _emit_item(
        db,
        hub,
        tenant_id,
        session_id,
        turn_id=turn_id,
        type="message",
        data={"role": "assistant", "content": reply},
    )
    stored = usage_from(usage)
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "completed"
        turn.usage = stored
        turn.updated_at = utc_now()
    if settings is not None:
        _row, limit_error = await harvest_session(
            db,
            settings,
            session_id,
            proc,
            turn_id=turn_id,
            blobs=blobs,
        )
        if limit_error is not None:
            if limit_error.code == "artifact_store":
                await _fail_turn(
                    db,
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
                )
                return
            await persist_event(
                db,
                hub,
                tenant_id,
                session_id,
                type="agent.session.error",
                data={"message": str(limit_error), "code": limit_error.code},
            )
    published = await artifact_bytes_for_turn(db, tenant_id, turn_id)
    await _write_turn_log(
        db,
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
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.completed",
        data={"turn_id": str(turn_id), "usage": stored},
    )
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


async def _cancel_turn(
    db: AsyncSession,
    hub: EventHub,
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
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "cancelled"
        turn.updated_at = utc_now()
    cancelled = failure_for("cancelled", "Cancelled")
    await _write_turn_log(
        db,
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
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.cancelled",
        data=cancel_data(str(turn_id)),
    )
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


def _lease_live(until: datetime | None) -> bool:
    if until is None:
        return False
    current = until if until.tzinfo is not None else until.replace(tzinfo=UTC)
    return current > utc_now()


async def fail_stale_in_progress(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    message: str = "Turn interrupted",
) -> SessionRow | None:
    row = await get_session(db, tenant_id, session_id)
    if row is None or row.status != "in_progress":
        return row
    if _lease_live(row.lease_until):
        return row
    turns = await list_turns(db, tenant_id, session_id)
    if turns:
        for turn in reversed(turns):
            if turn.status == "in_progress":
                await _fail_turn(
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
    store: Store,
    hub: EventHub,
    harness: Harness,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
) -> None:
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is None or row.status != "in_progress":
            return
    abort = hub.turn_abort(session_id)
    if abort is not None:
        abort.set()
        await harness.abort(session_id)
        for _ in range(100):
            async with store.session() as db:
                row = await get_session(db, tenant_id, session_id)
                if row is None or row.status != "in_progress":
                    return
            await asyncio.sleep(0.05)
    async with store.session() as db:
        await fail_stale_in_progress(db, hub, tenant_id, session_id)


async def _fail_turn(
    db: AsyncSession,
    hub: EventHub,
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
) -> None:
    resolved = failure or failure_for(code, message)
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "failed"
        turn.updated_at = utc_now()
    await _write_turn_log(
        db,
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
        data=session_error_data(resolved, mode=error_mode(settings)),
    )
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": []},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


def _cache_expected(row: SessionRow) -> bool:
    return row.pi_session_id is not None


def request_cancel(
    hub: EventHub, session_id: uuid.UUID, *, status: str
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


async def fail_session(
    db: AsyncSession,
    hub: EventHub,
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
    if code:
        data = session_error_data(failure_for(code, message), mode="legacy")
    else:
        data = {"message": message}
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=data,
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.failed")


async def fail_environment(
    db: AsyncSession,
    hub: EventHub,
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


async def load_boot_kwargs(
    store: Store,
    settings: Settings,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    mcp_http: list[Any] | None = None,
) -> dict[str, Any] | None:
    from apipi.worker.pi.platform_prompt import compose_instructions
    from apipi.worker.pi.sandbox import (
        image_for_size,
        mem_mib_for_size,
        sandbox_image_of,
        sandbox_size_of,
    )

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is None or not isinstance(row.environment, dict):
            return None
        if row.environment.get("type") != "openai_hosted":
            return None
        (
            _function_tools,
            model,
            instructions,
            _raw_tools,
            agent_metadata,
            agent_idle,
        ) = await _agent_tools_and_model(db, tenant_id, row)
        session_metadata = (
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        )
        ensure_openai_workspace(row.environment)
        gateway_allowlist = settings.microvm_egress_allowlist
        gateway_hosts: tuple[str, ...] = ()
        if settings.run_mode == "microvm":
            from apipi.worker.pi.microvm import microvm_egress_hosts

            gateway_hosts = tuple(microvm_egress_hosts(settings))
        backend = object_store(settings)
        extra_files = await FileService(store, backend, settings).workspace_files(
            tenant_id, row.environment
        )
        await provision_hosted_async(
            row.environment,
            run_mode=settings.run_mode,
            max_bytes=settings.max_workspace_bytes,
            gateway_allowlist=gateway_allowlist,
            gateway_hosts=gateway_hosts,
            extra_files=extra_files,
            timeout=settings.turn_timeout.total_seconds(),
        )
        directory = row.environment.get("directory")
        if isinstance(directory, str) and directory:
            await SkillService(store, backend, settings).install(
                tenant_id, row.environment, Path(directory)
            )
        builtin = _effective_builtin_tools(
            row.environment, session_metadata, agent_metadata
        )
        cwd_path, tools = _cwd_and_tools(row.environment, builtin)
        sandbox_size = sandbox_size_of(row.environment)
        stored_image = sandbox_image_of(row.environment)
        sandbox_image = stored_image or image_for_size(sandbox_size)
        composed = compose_instructions(
            settings,
            instructions,
            env_type="openai_hosted",
            sandbox_size=sandbox_size,
            mem_mib=mem_mib_for_size(settings, sandbox_size),
            network=_network_access(row.environment),
            builtin_tools=builtin,
        )
        kwargs: dict[str, Any] = {
            "cwd": cwd_path,
            "tools": tools,
            "mcp_http": mcp_http,
            "skill_dirs": _skill_dirs(row.environment, builtin),
            "tenant_id": tenant_id,
            "model": model,
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
        kwargs.update(
            _idle_spawn(
                settings,
                "openai_hosted",
                session_idle=row.idle_ttl,
                session_metadata=session_metadata,
                agent_idle=agent_idle,
            )
        )
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
    store: Store,
    hub: EventHub,
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
    objects: ObjectStore | None = None,
    blobs: ArtifactBlobs | None = None,
) -> None:
    abort = hub.watch_turn(session_id)
    if pool is not None:
        pool.hold(session_id)
    try:
        turn_id: uuid.UUID
        cwd_path: str | None
        tools: bool
        function_tools: list[dict[str, Any]]
        skill_dirs: list[str]
        model: str | None
        instructions: str | None
        env_type: str | None
        sandbox_mem: int | None
        sandbox_image: str
        extra_env: dict[str, str]
        agent_metadata: dict[str, Any]
        session_metadata: dict[str, Any]
        session_idle: str | None
        agent_idle: str | None
        spawn_ids = _spawn_identity_empty(user_id, org_id)
        item_content: str | list[dict[str, Any]] = text
        ordered = parts or []
        has_image = any(part.get("type") == "image" for part in ordered)
        if has_image and settings is not None and objects is not None:
            files = FileService(store, objects, settings)
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
                created = await files.create(
                    tenant_id,
                    data=data,
                    filename="image",
                    purpose="user_data",
                    content_type=mime,
                )
                stored_parts.append({"type": "input_image", "file_id": created["id"]})
            if len(stored_parts) == 1 and stored_parts[0].get("type") == "input_text":
                item_content = str(stored_parts[0].get("text") or text)
            elif stored_parts:
                item_content = stored_parts
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                return
            (
                function_tools,
                model,
                instructions,
                _raw_tools,
                agent_metadata,
                agent_idle,
            ) = await _agent_tools_and_model(db, tenant_id, row)
            session_metadata = (
                row.metadata_json if isinstance(row.metadata_json, dict) else {}
            )
            session_idle = row.idle_ttl
            ensure_openai_workspace(row.environment)
            try:
                model = _bind_turn_model(settings, model)
                gateway_allowlist = False
                gateway_hosts: tuple[str, ...] = ()
                extra_files: list[tuple[str, bytes]] = []
                backend = objects
                if settings is not None:
                    gateway_allowlist = settings.microvm_egress_allowlist
                    if settings.run_mode == "microvm":
                        from apipi.worker.pi.microvm import microvm_egress_hosts

                        gateway_hosts = tuple(microvm_egress_hosts(settings))
                    if backend is None:
                        backend = object_store(settings)
                    extra_files = await FileService(
                        store, backend, settings
                    ).workspace_files(tenant_id, row.environment)
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
                if settings is not None and backend is not None:
                    directory = row.environment.get("directory")
                    if isinstance(directory, str) and directory:
                        await SkillService(store, backend, settings).install(
                            tenant_id, row.environment, Path(directory)
                        )
            except (SetupError, ApiError) as exc:
                code = exc.code if isinstance(exc, ApiError) and exc.code else None
                await fail_environment(
                    db, hub, tenant_id, session_id, exc.message, code=code
                )
                return
            except ObjectStoreError:
                await fail_environment(
                    db,
                    hub,
                    tenant_id,
                    session_id,
                    "Cannot read artifacts",
                    code="artifact_store",
                )
                return
            builtin_tools = _effective_builtin_tools(
                row.environment, session_metadata, agent_metadata
            )
            cwd_path, tools = _cwd_and_tools(row.environment, builtin_tools)
            cache_error: ObjectStoreError | None = None
            if settings is not None and cwd_path:
                try:
                    await restore_pi_session(settings, row, Path(cwd_path), blobs=blobs)
                except ObjectStoreError as exc:
                    if _cache_expected(row):
                        cache_error = exc
                    else:
                        raise
            skill_dirs = _skill_dirs(row.environment, builtin_tools)
            env_type = row.environment.get("type")
            network = _network_access(row.environment)
            sandbox_size = sandbox_size_of(row.environment)
            sandbox_mem = (
                mem_mib_for_size(settings, sandbox_size)
                if settings is not None
                else None
            )
            stored_image = sandbox_image_of(row.environment)
            sandbox_image = stored_image or image_for_size(sandbox_size)
            extra_env = session_env_from(row.environment)
            spawn_ids = _spawn_identity(row, user_id, org_id)
            await update_session(
                db,
                tenant_id,
                session_id,
                changes={
                    "status": "in_progress",
                    "required_actions": [],
                },
            )
            await persist_event(
                db, hub, tenant_id, session_id, type="agent.session.in_progress"
            )
            turn = await create_turn(db, tenant_id, session_id, status="in_progress")
            turn_id = turn.id
            await persist_event(
                db,
                hub,
                tenant_id,
                session_id,
                type="agent.session.turn.created",
                data={"turn_id": str(turn_id)},
            )
            await persist_event(
                db,
                hub,
                tenant_id,
                session_id,
                type="agent.session.turn.in_progress",
                data={"turn_id": str(turn_id)},
            )
            await _emit_item(
                db,
                hub,
                tenant_id,
                session_id,
                turn_id=turn_id,
                type="message",
                data={"role": "user", "content": item_content},
            )
            if cache_error is not None:
                await _fail_turn(
                    db,
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
                    mcp_http=mcp_http,
                    skill_dirs=skill_dirs,
                    abort=abort,
                    tenant_id=tenant_id,
                    model=model,
                    instructions=composed,
                    api_key=api_key,
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
                    **_idle_spawn(
                        settings,
                        env_type,
                        session_idle=session_idle,
                        session_metadata=session_metadata,
                        agent_idle=agent_idle,
                    ),
                )
                retry_state = _new_retry_state()
                try:
                    if turn_timeout is None:
                        reply, pending, usage = await _consume_generate(
                            store,
                            hub,
                            tenant_id,
                            session_id,
                            turn_id,
                            generate,
                            retry_state,
                        )
                    else:
                        async with asyncio.timeout(turn_timeout.total_seconds()):
                            reply, pending, usage = await _consume_generate(
                                store,
                                hub,
                                tenant_id,
                                session_id,
                                turn_id,
                                generate,
                                retry_state,
                            )
                except CapacityError as exc:
                    async with store.session() as db:
                        await update_session(
                            db,
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
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
                        )
                    return
                except TimeoutError:
                    abort.set()
                    await harness.abort(session_id)
                    timed_out = replace(
                        failure_for("turn_timeout", "Turn timed out"),
                        upstream_attempts=_attempts_so_far(retry_state),
                    )
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
                        )
                    return
                except OSError as exc:
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
            async with store.session() as db:
                if abort.is_set():
                    await _cancel_turn(
                        db,
                        hub,
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
                if pending:
                    actions = pending
                    await update_session(
                        db,
                        tenant_id,
                        session_id,
                        changes={
                            "status": "requires_action",
                            "required_actions": actions,
                        },
                    )
                    await persist_event(
                        db,
                        hub,
                        tenant_id,
                        session_id,
                        type="agent.session.requires_action",
                        data={"turn_id": str(turn_id), "required_actions": actions},
                    )
                    return
                await _complete_turn(
                    db,
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
                    blobs=blobs,
                )
    finally:
        if pool is not None:
            pool.release(session_id)
        hub.unwatch_turn(session_id)


async def continue_turn(
    store: Store,
    hub: EventHub,
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
    blobs: ArtifactBlobs | None = None,
) -> None:
    cwd_path: str | None
    tools: bool
    function_tools: list[dict[str, Any]]
    skill_dirs: list[str]
    result: dict[str, Any]
    model: str | None = None
    instructions: str | None = None
    env_type: str | None
    spawn_ids = _spawn_identity_empty(user_id, org_id)
    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is None:
            raise ApiError(
                "invalid_request", "Not found", code="not_found", status_code=404
            )
        if row.status != "requires_action":
            raise ApiError(
                "invalid_request",
                "Session is not waiting for a tool result",
                code="invalid_request",
            )
        turn = await get_session_turn(db, tenant_id, session_id, turn_id)
        if turn is None or turn.status != "in_progress":
            raise ApiError(
                "invalid_request", "Not found", code="not_found", status_code=404
            )
        actions = [
            action for action in row.required_actions if isinstance(action, dict)
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
            if action.get("type") == "function_call"
            and action.get("call_id") != call_id
        ]
        if remaining:
            await update_session(
                db,
                tenant_id,
                session_id,
                changes={"required_actions": remaining},
            )
            return
        if pool is not None:
            pool.hold(session_id)
        ensure_openai_workspace(row.environment)
        cwd_path, tools = _cwd_and_tools(row.environment)
        if settings is not None and cwd_path:
            try:
                await restore_pi_session(settings, row, Path(cwd_path), blobs=blobs)
            except ObjectStoreError:
                if _cache_expected(row):
                    await _fail_turn(
                        db,
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
                    )
                    return
                raise
        (
            function_tools,
            model,
            instructions,
            _raw_tools,
            agent_metadata,
            agent_idle,
        ) = await _agent_tools_and_model(db, tenant_id, row)
        session_metadata = (
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        )
        session_idle = row.idle_ttl
        try:
            model = _bind_turn_model(settings, model)
        except ApiError as exc:
            await fail_environment(
                db, hub, tenant_id, session_id, exc.message, code=exc.code or None
            )
            return
        builtin_tools = _effective_builtin_tools(
            row.environment, session_metadata, agent_metadata
        )
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
    try:
        composed = compose_instructions(
            settings,
            instructions,
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
                    mcp_http=mcp_http,
                    skill_dirs=skill_dirs,
                    tenant_id=tenant_id,
                    model=model,
                    instructions=composed,
                    api_key=api_key,
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
                    **_idle_spawn(
                        settings,
                        env_type,
                        session_idle=session_idle,
                        session_metadata=session_metadata,
                        agent_idle=agent_idle,
                    ),
                )
                retry_state = _new_retry_state()
                try:
                    if turn_timeout is None:
                        reply, pending, usage = await _consume_generate(
                            store,
                            hub,
                            tenant_id,
                            session_id,
                            turn_id,
                            generate,
                            retry_state,
                        )
                    else:
                        async with asyncio.timeout(turn_timeout.total_seconds()):
                            reply, pending, usage = await _consume_generate(
                                store,
                                hub,
                                tenant_id,
                                session_id,
                                turn_id,
                                generate,
                                retry_state,
                            )
                except TurnFailed as exc:
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
                        )
                    return
                except TimeoutError:
                    await harness.abort(session_id)
                    timed_out = replace(
                        failure_for("turn_timeout", "Turn timed out"),
                        upstream_attempts=_attempts_so_far(retry_state),
                    )
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
                        )
                    return
                except OSError as exc:
                    async with store.session() as db:
                        await _fail_turn(
                            db,
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
            async with store.session() as db:
                if pending:
                    actions = pending
                    await update_session(
                        db,
                        tenant_id,
                        session_id,
                        changes={
                            "status": "requires_action",
                            "required_actions": actions,
                        },
                    )
                    await persist_event(
                        db,
                        hub,
                        tenant_id,
                        session_id,
                        type="agent.session.requires_action",
                        data={"turn_id": str(turn_id), "required_actions": actions},
                    )
                    return
                await _complete_turn(
                    db,
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
                    blobs=blobs,
                )
    finally:
        if pool is not None:
            pool.release(session_id)
