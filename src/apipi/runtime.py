import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.errors import ApiError
from apipi.skills import discover_skill_dirs
from apipi.store.engine import Store
from apipi.store.events import append_event, list_events
from apipi.store.models import Event, utc_now
from apipi.store.repo import (
    create_item,
    create_turn,
    get_agent,
    get_session,
    get_session_turn,
    list_items,
    update_session,
)
from apipi.store.turn_logs import append_turn_log
from apipi.usage import add_usage, empty_usage, usage_from

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
        "agent.session.environment.pending",
        "agent.session.environment.connected",
        "agent.session.environment.disconnected",
        "agent.session.environment.failed",
    }
)


def event_body(event: Event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "type": event.type,
        "seq": event.seq,
        "session_id": str(event.session_id),
        "created_at": event.created_at.isoformat(),
        "data": event.data,
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
        self.mcp_stdio: list[Any] | None = None
        self.skill_dirs: list[str] | None = None
        self.tools: bool | None = None
        self.hold = False
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
        mcp_stdio: list[Any] | None = None,
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
        self.function_tools = (
            list(function_tools) if function_tools is not None else None
        )
        self.mcp_http = list(mcp_http) if mcp_http is not None else None
        self.mcp_stdio = list(mcp_stdio) if mcp_stdio is not None else None
        self.skill_dirs = list(skill_dirs) if skill_dirs is not None else None
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


def _cwd_and_tools(environment: dict[str, Any]) -> tuple[str | None, bool]:
    env_type = environment.get("type")
    if env_type in {"none", "self_hosted"}:
        return None, False
    cwd = environment.get("directory")
    cwd_path = cwd if isinstance(cwd, str) else None
    return cwd_path, True


def with_env_actions(current: list[Any], actions: list[Any]) -> list[Any]:
    env = [
        item
        for item in current
        if isinstance(item, dict) and item.get("type") == "environment_connection"
    ]
    rest = [
        item
        for item in actions
        if isinstance(item, dict) and item.get("type") != "environment_connection"
    ]
    return rest + env


def _skill_dirs(environment: dict[str, Any]) -> list[str]:
    cwd_path, _tools = _cwd_and_tools(environment)
    workspace = Path(cwd_path) if cwd_path is not None else None
    raw = environment.get("capability_directories")
    directories = None
    if isinstance(raw, list):
        directories = [item for item in raw if isinstance(item, str)]
    return discover_skill_dirs(workspace, directories)


async def _agent_function_tools(
    db: AsyncSession, tenant_id: uuid.UUID, agent_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    if agent_id is None:
        return []
    agent = await get_agent(db, tenant_id, agent_id)
    if agent is None:
        return []
    return _function_tools(agent.tools)


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


async def _consume_generate(
    store: Store,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    events: AsyncIterator[tuple[str, dict[str, Any]]],
) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    reply = ""
    pending: list[dict[str, Any]] = []
    usage = empty_usage()
    async for etype, data in events:
        if etype == "usage":
            usage = add_usage(usage, usage_from(data))
            continue
        payload = dict(data)
        payload.setdefault("turn_id", str(turn_id))
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


def _mcp_name(raw: object, labels: list[str]) -> str | None:
    if not isinstance(raw, str) or raw == "":
        return None
    for label in labels:
        if label in raw:
            return label
    return raw


async def _tool_mcp_for_turn(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    labels: list[str],
) -> tuple[list[str], dict[str, int], list[str], dict[str, int]]:
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
        if event.type != "agent.session.turn.item.added":
            continue
        if event.data.get("turn_id") != str(turn_id):
            continue
        if event.data.get("item_type") != "mcp_call":
            continue
        name = _mcp_name(event.data.get("name"), labels)
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
) -> None:
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is None:
        return
    row = await get_session(db, tenant_id, session_id)
    agent_id = row.agent_id if row is not None else None
    model: str | None = None
    labels: list[str] = []
    if agent_id is not None:
        agent = await get_agent(db, tenant_id, agent_id)
        if agent is not None:
            model = agent.model
            labels = _mcp_labels(agent.tools)
    stored = usage_from(usage)
    tool_names, tool_counts, mcp_names, mcp_counts = await _tool_mcp_for_turn(
        db, tenant_id, session_id, turn_id, labels
    )
    await append_turn_log(
        db,
        tenant_id,
        session_id,
        turn_id,
        status=status,
        agent_id=agent_id,
        model=model,
        latency_ms=_latency_ms(turn.created_at),
        prompt_tokens=stored["prompt_tokens"],
        completion_tokens=stored["completion_tokens"],
        cache_read_tokens=stored["cache_read_tokens"],
        cache_write_tokens=stored["cache_write_tokens"],
        total_tokens=stored["total_tokens"],
        error_code=error_code,
        tool_names=tool_names,
        tool_counts=tool_counts,
        mcp_names=mcp_names,
        mcp_counts=mcp_counts,
    )


async def _complete_turn(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    reply: str,
    usage: dict[str, int],
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
    await _write_turn_log(
        db,
        tenant_id,
        session_id,
        turn_id,
        status="completed",
        usage=stored,
    )
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.completed",
        data={"turn_id": str(turn_id), "usage": stored},
    )
    row = await get_session(db, tenant_id, session_id)
    current = row.required_actions if row is not None else []
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": with_env_actions(current, [])},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


async def _cancel_turn(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
) -> None:
    turn = await get_session_turn(db, tenant_id, session_id, turn_id)
    if turn is not None:
        turn.status = "cancelled"
        turn.updated_at = utc_now()
    await _write_turn_log(db, tenant_id, session_id, turn_id, status="cancelled")
    await persist_event(
        db,
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.cancelled",
        data={"turn_id": str(turn_id)},
    )
    row = await get_session(db, tenant_id, session_id)
    current = row.required_actions if row is not None else []
    await update_session(
        db,
        tenant_id,
        session_id,
        changes={"status": "idle", "required_actions": with_env_actions(current, [])},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.idle")


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


async def fail_session(
    db: AsyncSession,
    hub: EventHub,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    message: str,
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
        data={"message": message},
    )
    await persist_event(db, hub, tenant_id, session_id, type="agent.session.failed")


async def run_turn(
    store: Store,
    hub: EventHub,
    harness: Harness,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    text: str,
    *,
    mcp_http: list[Any] | None = None,
    mcp_stdio: list[Any] | None = None,
) -> None:
    abort = hub.watch_turn(session_id)
    try:
        turn_id: uuid.UUID
        cwd_path: str | None
        tools: bool
        function_tools: list[dict[str, Any]]
        skill_dirs: list[str]
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None:
                return
            cwd_path, tools = _cwd_and_tools(row.environment)
            function_tools = await _agent_function_tools(db, tenant_id, row.agent_id)
            skill_dirs = _skill_dirs(row.environment)
            await update_session(
                db,
                tenant_id,
                session_id,
                changes={
                    "status": "in_progress",
                    "required_actions": with_env_actions(row.required_actions, []),
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
                data={"role": "user", "content": text},
            )
        reply, pending, usage = await _consume_generate(
            store,
            hub,
            tenant_id,
            session_id,
            turn_id,
            harness.generate(
                text,
                session_id=session_id,
                cwd=cwd_path,
                tools=tools,
                function_tools=function_tools,
                mcp_http=mcp_http,
                mcp_stdio=mcp_stdio,
                skill_dirs=skill_dirs,
                abort=abort,
            ),
        )
        async with store.session() as db:
            if abort.is_set():
                await _cancel_turn(db, hub, tenant_id, session_id, turn_id)
                return
            if pending:
                latest = await get_session(db, tenant_id, session_id)
                current = latest.required_actions if latest is not None else []
                actions = with_env_actions(current, pending)
                await update_session(
                    db,
                    tenant_id,
                    session_id,
                    changes={"status": "requires_action", "required_actions": actions},
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
            await _complete_turn(db, hub, tenant_id, session_id, turn_id, reply, usage)
    finally:
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
    mcp_stdio: list[Any] | None = None,
) -> None:
    cwd_path: str | None
    tools: bool
    function_tools: list[dict[str, Any]]
    skill_dirs: list[str]
    result: dict[str, Any]
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
                changes={
                    "required_actions": with_env_actions(
                        row.required_actions, remaining
                    )
                },
            )
            return
        cwd_path, tools = _cwd_and_tools(row.environment)
        function_tools = await _agent_function_tools(db, tenant_id, row.agent_id)
        skill_dirs = _skill_dirs(row.environment)
        result = {
            "call_id": call_id,
            "success": success,
            "output": output,
            "error": error,
        }
    reply, pending, usage = await _consume_generate(
        store,
        hub,
        tenant_id,
        session_id,
        turn_id,
        harness.generate(
            "",
            session_id=session_id,
            cwd=cwd_path,
            tools=tools,
            function_tools=function_tools,
            tool_result=result,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        ),
    )
    async with store.session() as db:
        if pending:
            latest = await get_session(db, tenant_id, session_id)
            current = latest.required_actions if latest is not None else []
            actions = with_env_actions(current, pending)
            await update_session(
                db,
                tenant_id,
                session_id,
                changes={"status": "requires_action", "required_actions": actions},
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
        await _complete_turn(db, hub, tenant_id, session_id, turn_id, reply, usage)
