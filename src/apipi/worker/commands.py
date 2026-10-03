import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from apipi.common.errors import ApiError
from apipi.common.failures import (
    failure_for,
    log_extra,
    log_level_for_code,
    session_error_data,
    turn_failed_data,
)
from apipi.common.logutil import log_event
from apipi.common.otel import (
    attach_traceparent,
    detach_traceparent,
)
from apipi.common.placement import worker_accepts
from apipi.protocol import (
    COMMAND_CONTEXT_OPS,
    COMMAND_OPS,
    COMMAND_PAYLOAD_MODELS,
    BaseCommandPayload,
    ContextBytes,
    ContextCommandPayload,
    SessionStoppedPayload,
    TurnContinueCommandPayload,
    TurnStartCommandPayload,
    WorkerCommand,
    parse_turn_context,
    summarize_context,
)
from apipi.worker.runtime import report_session_failed

log = logging.getLogger("apipi.worker")

_TURN_OPS = frozenset({"turn.start", "turn.continue"})


class CommandDedupe:
    """Remember recent command ids per session (bounded, worker-side).

    A retransmitted command (same `id`) is acked again but never
    dispatched twice, so a duplicate `turn.start` cannot start a
    second turn.
    """

    def __init__(self, limit: int = 128) -> None:
        self.limit = max(limit, 1)
        self._seen: dict[uuid.UUID, list[str]] = {}

    def duplicate(self, session_id: uuid.UUID, command_id: str) -> bool:
        known = self._seen.setdefault(session_id, [])
        if command_id in known:
            return True
        known.append(command_id)
        del known[: max(len(known) - self.limit, 0)]
        return False

    def discard(self, session_id: uuid.UUID, command_id: str) -> None:
        """Forget one command id, so a retransmit runs the command again."""
        known = self._seen.get(session_id)
        if known is not None and command_id in known:
            known.remove(command_id)

    def forget(self, session_id: uuid.UUID) -> None:
        self._seen.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._seen)


def _sink_for_execution(
    execution: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> Any:
    return execution.sink_for(tenant_id, session_id)


async def _reject_missing_image(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    image: str,
    request_id: str | None,
) -> None:
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    message = (
        f'No worker has sandbox_image "{image}". Run apipi images pull on a worker.'
    )
    log_event(
        log,
        logging.WARNING,
        "worker image missing",
        event="worker.placement.rejected",
        error_code="image_unavailable",
        tenant_id=tenant_id,
        session_id=session_id,
        request_id=request_id,
    )
    active = _sink_for_execution(execution, tenant_id, session_id)
    await active.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data={"message": message, "code": "image_unavailable"},
    )
    await active.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.failed",
        data={"message": message},
    )


async def _reject_mismatched_turn(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    required: str,
    worker_mode: str,
    request_id: str | None,
) -> None:
    log_event(
        log,
        logging.WARNING,
        "worker placement rejected",
        event="worker.placement.rejected",
        error_code="placement",
        tenant_id=tenant_id,
        session_id=session_id,
        request_id=request_id,
        run_mode=worker_mode,
    )
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    message = f"Worker does not accept {required} sessions"
    active = _sink_for_execution(execution, tenant_id, session_id)
    await active.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.error",
        data=session_error_data(
            failure_for("placement", message),
            mode="legacy",
        ),
    )
    placed = failure_for("placement", message)
    failed = turn_failed_data("", placed)
    failed.pop("turn_id", None)
    await active.append_event(
        hub,
        tenant_id,
        session_id,
        type="agent.session.turn.failed",
        data=failed,
    )


def _command_turn_context(
    op: object, payload: BaseCommandPayload
) -> dict[str, Any] | None:
    """Validate the command context on the worker; invalid fails the turn."""
    if op not in COMMAND_CONTEXT_OPS:
        return None
    raw = payload.context if isinstance(payload, ContextCommandPayload) else None
    if raw is None:
        return None
    try:
        return parse_turn_context(raw).model_dump()
    except (ContextBytes, ValidationError) as exc:
        raise ApiError(
            "invalid_request",
            f"invalid turn context: {exc}",
            code="invalid_request",
            status_code=400,
        ) from exc


def command_log_context(command: WorkerCommand) -> dict[str, Any]:
    """Secret-free context summary for the worker command log."""
    raw = command.payload.get("context")
    if not isinstance(raw, dict):
        return {}
    return {"context": summarize_context(raw)}


async def _report_escaped_turn(
    execution: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    exc: BaseException,
) -> None:
    hub = getattr(execution, "hub", None)
    if hub is None:
        return
    if isinstance(exc, ApiError):
        message = exc.message
        code = exc.code or "internal"
    else:
        message = "Turn failed"
        code = "internal"
    await report_session_failed(
        _sink_for_execution(execution, tenant_id, session_id),
        hub,
        tenant_id,
        session_id,
        message,
        code=code,
    )


def _typed_payload(
    op: object, payload: BaseCommandPayload | dict[str, Any]
) -> BaseCommandPayload:
    if isinstance(payload, BaseCommandPayload):
        return payload
    model = COMMAND_PAYLOAD_MODELS.get(op if isinstance(op, str) else "")
    return (model or BaseCommandPayload).model_validate(payload)


async def dispatch_command(
    execution: Any, message: WorkerCommand | dict[str, Any]
) -> str:
    """Run one command; returns `dispatched`, `rejected`, or `failed`.

    An exception that is not a turn failure propagates to the caller.
    """
    raw_op = message.op if isinstance(message, WorkerCommand) else message.get("op")
    op_label = (
        raw_op if isinstance(raw_op, str) and raw_op in COMMAND_OPS else "unknown"
    )
    metrics = getattr(execution, "metrics", None)
    started = time.monotonic()
    result = "failed"
    try:
        result = await _dispatch(execution, message)
    except asyncio.CancelledError:
        result = "dispatched"
        raise
    finally:
        if metrics is not None:
            metrics.observe_worker_command_received(op_label, result)
            metrics.observe_worker_command_seconds(op_label, time.monotonic() - started)
    return result


def log_command(command: WorkerCommand) -> None:
    """Info line for one accepted command, with the secret-free context summary."""
    request_id = command.payload.get("request_id")
    traceparent = command.payload.get("traceparent")
    log_event(
        log,
        logging.INFO,
        "worker command",
        event="worker.command",
        op=command.op,
        session_id=command.session_id,
        lease_id=command.lease_id,
        command_id=command.command_id,
        request_id=request_id if isinstance(request_id, str) else None,
        traceparent=traceparent if isinstance(traceparent, str) else None,
        **command_log_context(command),
    )


async def _dispatch(execution: Any, message: WorkerCommand | dict[str, Any]) -> str:
    if isinstance(message, WorkerCommand):
        op: object = message.op
        raw_session: object = message.session_id
        raw_payload: object = message.payload
    else:
        op = message.get("op")
        raw_session = message.get("session_id")
        raw_payload = message.get("payload")
    try:
        session_id = (
            raw_session
            if isinstance(raw_session, uuid.UUID)
            else uuid.UUID(str(raw_session))
        )
        payload = _typed_payload(
            op, raw_payload if isinstance(raw_payload, dict) else {}
        )
    except (ValueError, ValidationError):
        log.warning(
            "worker command invalid",
            extra={"event": "worker.command.invalid"},
        )
        return "rejected"
    tenant_id = payload.tenant_id
    if tenant_id is None:
        return "rejected"
    request_id = payload.request_id
    key_id = payload.key_id
    user_id = payload.user_id
    org_id = payload.org_id
    token = attach_traceparent(payload.traceparent)
    try:
        turn_context = _command_turn_context(op, payload)
        outcome = await _run_command(
            execution,
            op,
            tenant_id,
            session_id,
            payload,
            request_id=request_id,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
        )
    except ApiError as exc:
        command_failure = failure_for(exc.code or "internal", exc.message)
        log_event(
            log,
            log_level_for_code(command_failure.code, command_failure.failure_source),
            "worker command failed",
            event="worker.command.failed",
            exc_info=exc,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            **log_extra(command_failure),
        )
        if op in _TURN_OPS:
            await _report_escaped_turn(execution, tenant_id, session_id, exc)
            return "failed"
        raise
    except Exception as exc:
        internal = failure_for("internal", "Turn failed")
        log_event(
            log,
            logging.ERROR,
            "worker command failed",
            event="worker.command.failed",
            exc_info=exc,
            tenant_id=tenant_id,
            session_id=session_id,
            request_id=request_id,
            **log_extra(internal),
        )
        if op in _TURN_OPS:
            await _report_escaped_turn(execution, tenant_id, session_id, exc)
            return "failed"
        raise
    finally:
        detach_traceparent(token)
    return outcome


async def _run_command(
    execution: Any,
    op: object,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: BaseCommandPayload | dict[str, Any],
    *,
    request_id: str | None,
    key_id: str | None,
    user_id: str | None,
    api_key: str | None = None,
    org_id: str | None = None,
    turn_context: dict[str, Any] | None = None,
) -> str:
    body = _typed_payload(op, payload)
    if op == "turn.start" and isinstance(body, TurnStartCommandPayload):
        required = body.run_mode
        settings = getattr(execution, "settings", None)
        if isinstance(required, str) and settings is not None:
            from apipi.worker.accepts import resolved_worker_accepts

            accepts = resolved_worker_accepts(settings)
            if not worker_accepts(accepts, required):
                await _reject_mismatched_turn(
                    execution,
                    tenant_id,
                    session_id,
                    required=required,
                    worker_mode=",".join(sorted(accepts)),
                    request_id=request_id,
                )
                return "rejected"
        wanted = body.sandbox_image
        worker_mode = (
            getattr(settings, "run_mode", None) if settings is not None else None
        )
        if (
            isinstance(wanted, str)
            and worker_mode == "microvm"
            and isinstance(getattr(execution, "settings", None), object)
        ):
            from apipi.common.images import available_images

            have = {item.id for item in available_images(execution.settings)}
            if wanted not in have:
                await _reject_missing_image(
                    execution,
                    tenant_id,
                    session_id,
                    image=wanted,
                    request_id=request_id,
                )
                return "rejected"
        await execution.run_turn(
            tenant_id,
            session_id,
            body.text if body.text is not None else "",
            images=body.images,
            parts=body.parts,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return "dispatched"
    if op == "turn.continue" and isinstance(body, TurnContinueCommandPayload):
        if body.turn_id is None or body.call_id is None or body.success is None:
            return "rejected"
        await execution.continue_turn(
            tenant_id,
            session_id,
            turn_id=body.turn_id,
            call_id=body.call_id,
            success=body.success,
            output=body.output,
            error=body.error,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return "dispatched"
    if op == "turn.cancel":
        await execution.cancel(session_id, status="in_progress")
        return "dispatched"
    if op == "session.stop":
        await execution.teardown(session_id)
        await _wipe_stopped_session(execution, tenant_id, session_id)
        return "dispatched"
    if op == "sandbox.boot":
        await execution.boot_hosted(tenant_id, session_id, turn_context=turn_context)
        return "dispatched"
    return "rejected"


async def _wipe_stopped_session(
    execution: Any, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> None:
    from apipi.common.dirs import wipe_workspace

    # The workspace directory follows the shared sessions-root layout,
    # the blob delete happens on the API when it ingests
    # `session.stopped`, and the envelope is the durable receipt.
    base = getattr(execution.settings, "sessions_dir", "")
    root = Path(base) if base else Path.cwd() / ".apipi" / "sessions"
    wipe_workspace(root / str(tenant_id) / str(session_id))
    try:
        execution.outbox.append(
            session_id, "session.stopped", SessionStoppedPayload(reason="stop")
        )
    except Exception:
        log.exception(
            "session stopped report failed",
            extra={"session_id": str(session_id)},
        )
