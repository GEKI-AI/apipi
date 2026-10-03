import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from apipi.common.failures import (
    Failure,
    log_extra,
    log_level_for,
    usage_fields,
)
from apipi.common.ids import uuid_or_none
from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics, observe_turn
from apipi.common.otel import Tracing, set_span
from apipi.common.usage import mcp_name, tally, usage_event, usage_from
from apipi.config import Settings
from apipi.protocol import PUBLIC_EVENT_TYPES as PUBLIC_EVENT_TYPES
from apipi.protocol import (
    TurnContext,
)
from apipi.services.agents import definition_for_session
from apipi.services.payload_export import export_payload
from apipi.services.session_events import event_body as event_body
from apipi.services.session_events import persist_event as persist_event
from apipi.services.usage_export import export_usage
from apipi.store.events import list_events
from apipi.store.models import utc_now
from apipi.store.repo import (
    add_usage_rollup,
    append_turn_log,
    get_session,
    get_session_turn,
    list_items,
    lock_turn,
    search_usage_for_turn,
)

log = logging.getLogger("apipi")


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
        name = mcp_name(event.data)
        if name is not None:
            mcp_names.append(name)
    tools, tool_counts = tally(tool_names)
    mcps, mcp_counts = tally(mcp_names)
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
    await lock_turn(db, tenant_id, session_id, turn_id)
    search_calls, search_units, search_counts = await search_usage_for_turn(
        db, tenant_id, turn_id
    )
    if turn_context is not None:
        agent_id = uuid_or_none(turn_context.session.agent_id)
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
        search_calls=search_calls,
        search_units=search_units,
        search_counts=search_counts,
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
            search_calls=search_calls,
            search_units=search_units,
            search_counts=search_counts,
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
            search_calls=search_calls,
            search_units=search_units,
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
