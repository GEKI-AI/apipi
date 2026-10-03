import logging
import uuid
from typing import Any

from apipi.common.errors import ObjectStoreError
from apipi.common.event_bus import EventBus
from apipi.common.failures import (
    Failure,
    cancel_data,
    error_mode,
    failure_for,
    session_error_data,
    turn_failed_data,
)
from apipi.common.metrics import Metrics
from apipi.common.otel import Tracing
from apipi.common.usage import usage_from
from apipi.config import Settings
from apipi.protocol import PUBLIC_EVENT_TYPES as PUBLIC_EVENT_TYPES
from apipi.protocol import (
    TurnContext,
)
from apipi.worker.outbox import OutboxFull
from apipi.worker.pi.proc import PiProc
from apipi.worker.sink import ResultSink

log = logging.getLogger("apipi")


async def harvest_split(
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
            await fail_turn(
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


async def complete_turn(
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
    await emit_item(
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
        harvested = await harvest_split(
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


async def cancel_turn(
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


async def fail_outbox_full(
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

    abort = hub.turn_abort(session_id)
    if abort is not None:
        abort.set()
    try:
        async with sink.emergency_mode():
            await fail_turn(
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


async def fail_turn(
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


async def emit_item(
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
