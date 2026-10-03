import logging
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
    ContextBytes,
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

    def forget(self, session_id: uuid.UUID) -> None:
        self._seen.pop(session_id, None)


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


def _command_turn_context(op: object, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the command context on the worker; invalid fails the turn."""
    if op not in COMMAND_CONTEXT_OPS:
        return None
    raw = payload.get("context")
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


def command_log_context(message: dict[str, Any]) -> dict[str, Any]:
    """Secret-free context summary for the worker command log."""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return {}
    raw = payload.get("context")
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


async def dispatch_command(execution: Any, message: dict[str, Any]) -> None:
    op = message.get("op")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    try:
        session_id = uuid.UUID(str(message.get("session_id")))
        tenant_id = uuid.UUID(str(payload.get("tenant_id")))
    except (ValueError, TypeError):
        return
    request_id = payload.get("request_id")
    api_key = payload.get("api_key")
    key_id = payload.get("key_id")
    user_id = payload.get("user_id")
    org_id = payload.get("org_id")
    request_id = request_id if isinstance(request_id, str) else None
    api_key = api_key if isinstance(api_key, str) else None
    key_id = key_id if isinstance(key_id, str) else None
    user_id = user_id if isinstance(user_id, str) else None
    org_id = org_id if isinstance(org_id, str) else None
    raw_parent = payload.get("traceparent")
    token = attach_traceparent(raw_parent if isinstance(raw_parent, str) else None)
    try:
        turn_context = _command_turn_context(op, payload)
        await _run_command(
            execution,
            op,
            tenant_id,
            session_id,
            payload,
            request_id=request_id,
            api_key=api_key,
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
            return
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
            return
        raise
    finally:
        detach_traceparent(token)


async def _run_command(
    execution: Any,
    op: object,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    request_id: str | None,
    api_key: str | None,
    key_id: str | None,
    user_id: str | None,
    org_id: str | None = None,
    turn_context: dict[str, Any] | None = None,
) -> None:
    if op == "turn.start":
        required = payload.get("run_mode")
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
                return
        wanted = payload.get("sandbox_image")
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
                return
        text = payload.get("text")
        raw_images = payload.get("images")
        images = raw_images if isinstance(raw_images, list) else None
        raw_parts = payload.get("parts")
        parts = raw_parts if isinstance(raw_parts, list) else None
        await execution.run_turn(
            tenant_id,
            session_id,
            text if isinstance(text, str) else "",
            images=images,
            parts=parts,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return
    if op == "turn.continue":
        raw_turn = payload.get("turn_id")
        call_id = payload.get("call_id")
        success = payload.get("success")
        if not isinstance(raw_turn, str) or not isinstance(call_id, str):
            return
        if not isinstance(success, bool):
            return
        output = payload.get("output")
        error = payload.get("error")
        await execution.continue_turn(
            tenant_id,
            session_id,
            turn_id=uuid.UUID(raw_turn),
            call_id=call_id,
            success=success,
            output=output if isinstance(output, str) else None,
            error=error if isinstance(error, str) else None,
            request_id=request_id,
            api_key=api_key,
            key_id=key_id,
            user_id=user_id,
            org_id=org_id,
            turn_context=turn_context,
            sink=_sink_for_execution(execution, tenant_id, session_id),
        )
        return
    if op == "turn.cancel":
        await execution.cancel(session_id, status="in_progress")
        return
    if op == "session.stop":
        await execution.teardown(session_id)
        await _wipe_stopped_session(execution, tenant_id, session_id)
        return
    if op == "sandbox.boot":
        await execution.boot_hosted(tenant_id, session_id, turn_context=turn_context)


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
        execution.outbox.append(session_id, "session.stopped", {"reason": "stop"})
    except Exception:
        log.exception(
            "session stopped report failed",
            extra={"session_id": str(session_id)},
        )
