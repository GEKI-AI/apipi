"""ResultSink: where turn results are written.

The turn runtime reports turns, items, events, usage, and errors
through this interface. The worker uses :class:`OutboxSink`, which
buffers durable protocol v2 envelopes in
:class:`apipi.worker.outbox.Outbox` until the API ingests them and
sends the cumulative ack. The per-turn tool and MCP tallies are
collected in memory on the sink while the turn runs, so the worker
never reads items or events back to summarize the turn.
"""

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, Protocol

from apipi.common.logutil import log_event
from apipi.common.usage import tally
from apipi.protocol import (
    PUBLIC_EVENT_TYPES,
    ItemAddedPayload,
    SessionStatusPayload,
    TurnStatusPayload,
    UsageFailure,
    UsagePayload,
    WorkerEventPayload,
)

log = logging.getLogger("apipi")


class TurnTally:
    """In-memory tool and MCP counts for the running turn."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._tools: list[str] = []
        self._mcps: list[str] = []

    def add_tool(self, name: str) -> None:
        if name:
            self._tools.append(name)

    def add_mcp(self, name: str | None) -> None:
        if name:
            self._mcps.append(name)

    def snapshot(self) -> tuple[list[str], dict[str, int], list[str], dict[str, int]]:
        tools, tool_counts = tally(self._tools)
        mcps, mcp_counts = tally(self._mcps)
        return tools, tool_counts, mcps, mcp_counts

    def empty(self) -> bool:
        return not self._tools and not self._mcps


class ResultSink(Protocol):
    """Write side of the turn runtime."""

    def emergency_mode(self) -> AbstractAsyncContextManager[None, bool | None]:
        """Spend the reserved budget so a full outbox can still fail."""
        ...

    def tally_tool(self, name: str) -> None: ...
    def tally_mcp(self, name: str | None) -> None: ...
    def tally_snapshot(
        self,
    ) -> tuple[list[str], dict[str, int], list[str], dict[str, int]]: ...

    async def update_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        changes: dict[str, Any],
        user_id: str | None = None,
    ) -> Any: ...

    async def create_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        turn_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...

    async def create_item(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        data: dict[str, Any] | None = None,
        turn_id: uuid.UUID | None = None,
        item_id: uuid.UUID | None = None,
    ) -> uuid.UUID: ...

    async def append_event(
        self,
        hub: Any,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> None: ...

    async def finish_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        failure: Any | None = None,
    ) -> None: ...

    async def write_turn_log(
        self,
        hub: Any,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, int] | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        metrics: Any | None = None,
        tracing: Any | None = None,
        settings: Any | None = None,
        artifact_bytes: int = 0,
        user_id: str | None = None,
        failure: Any | None = None,
        turn_context: Any | None = None,
    ) -> None: ...

    async def store_input_image(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        data: bytes,
        filename: str,
        content_type: str | None,
        settings: Any | None = None,
    ) -> dict[str, Any]: ...

    async def store_artifacts(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        files: list[tuple[str, bytes]],
        *,
        turn_id: uuid.UUID | None = None,
        settings: Any | None = None,
    ) -> None: ...

    async def store_pi_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        data: bytes,
        *,
        settings: Any | None = None,
    ) -> None: ...


class OutboxSink:
    """Buffer durable v2 envelopes instead of writing to the database.

    Turn context arrives in the command, so no database reads are
    needed here either; every write below only appends to the outbox.
    The worker sends buffered envelopes over `/internal/worker` and
    drops them when the cumulative ack arrives.
    """

    def __init__(
        self,
        outbox: Any,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        settings: Any | None = None,
        metrics: Any | None = None,
        tracing: Any | None = None,
        waiters: dict[Any, Any] | None = None,
    ) -> None:
        self.outbox = outbox
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.settings = settings
        self.metrics = metrics
        self.tracing = tracing
        self.waiters = waiters if waiters is not None else {}
        self._tally = TurnTally()
        self._turn_started: dict[uuid.UUID, float] = {}
        self._emergency = False

    @asynccontextmanager
    async def emergency_mode(self) -> AsyncIterator[None]:
        previous = self._emergency
        self._emergency = True
        try:
            yield
        finally:
            self._emergency = previous

    def _check(self, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
        if tenant_id != self.tenant_id or session_id != self.session_id:
            raise ValueError("OutboxSink bound to another session")

    def tally_tool(self, name: str) -> None:
        self._tally.add_tool(name)

    def tally_mcp(self, name: str | None) -> None:
        self._tally.add_mcp(name)

    def tally_snapshot(
        self,
    ) -> tuple[list[str], dict[str, int], list[str], dict[str, int]]:
        return self._tally.snapshot()

    async def update_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        changes: dict[str, Any],
        user_id: str | None = None,
    ) -> Any:
        del user_id
        self._check(tenant_id, session_id)
        self.outbox.append(
            session_id,
            "session.status",
            SessionStatusPayload(
                status=changes.get("status"),
                required_actions=changes.get("required_actions", []),
            ),
            emergency=self._emergency,
        )
        return None

    async def create_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        turn_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        from apipi.worker.outbox import OutboxFull

        del usage
        self._check(tenant_id, session_id)
        resolved = turn_id if turn_id is not None else uuid.uuid4()
        self._tally.reset()
        self._turn_started[resolved] = time.monotonic()
        try:
            self.outbox.append(
                session_id,
                "turn.status",
                TurnStatusPayload.model_validate(
                    {
                        "turn_id": resolved,
                        "status": "started" if status == "in_progress" else status,
                    }
                ),
                turn_id=resolved,
                emergency=self._emergency,
            )
        except OutboxFull:
            self._turn_started.pop(resolved, None)
            raise
        return resolved

    async def create_item(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        data: dict[str, Any] | None = None,
        turn_id: uuid.UUID | None = None,
        item_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        self._check(tenant_id, session_id)
        resolved = item_id if item_id is not None else uuid.uuid4()
        self.outbox.append(
            session_id,
            "item.added",
            ItemAddedPayload(
                item_id=resolved,
                item_type=type,
                turn_id=turn_id,
                data=data if data is not None else {},
            ),
            turn_id=turn_id,
            emergency=self._emergency,
        )
        return resolved

    async def append_event(
        self,
        hub: Any,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        del hub
        self._check(tenant_id, session_id)
        if type not in PUBLIC_EVENT_TYPES:
            return None
        body = data if data is not None else {}
        raw_turn = body.get("turn_id")
        turn_id: uuid.UUID | None = None
        if isinstance(raw_turn, str) and raw_turn:
            try:
                turn_id = uuid.UUID(raw_turn)
            except ValueError:
                turn_id = None
        self.outbox.append(
            session_id,
            "event",
            WorkerEventPayload(type=type, data=body, turn_id=turn_id),
            turn_id=turn_id,
            emergency=self._emergency,
        )
        return None

    async def finish_turn(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        failure: Any | None = None,
    ) -> None:
        del usage
        self._check(tenant_id, session_id)
        status_payload = TurnStatusPayload.model_validate(
            {"turn_id": turn_id, "status": status}
        )
        if failure is not None:
            status_payload.code = failure.code
            status_payload.message = failure.message
        self.outbox.append(
            session_id,
            "turn.status",
            status_payload,
            turn_id=turn_id,
            emergency=self._emergency or status in {"failed", "cancelled"},
        )

    async def write_turn_log(
        self,
        hub: Any,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        *,
        status: str,
        usage: dict[str, int] | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        metrics: Any | None = None,
        tracing: Any | None = None,
        settings: Any | None = None,
        artifact_bytes: int = 0,
        user_id: str | None = None,
        failure: Any | None = None,
        turn_context: Any | None = None,
    ) -> None:
        from apipi.common.failures import failure_dict, log_extra, log_level_for
        from apipi.common.usage import usage_from

        del hub, turn_context
        self._check(tenant_id, session_id)
        stored = usage_from(usage)
        tools, tool_counts, mcp_names, mcp_counts = self._tally.snapshot()
        started = self._turn_started.get(turn_id)
        latency_ms = max(int((time.monotonic() - started) * 1000), 0) if started else 0
        usage_payload = UsagePayload(
            turn_id=turn_id,
            status=status,
            prompt_tokens=stored["prompt_tokens"],
            completion_tokens=stored["completion_tokens"],
            cache_read_tokens=stored["cache_read_tokens"],
            cache_write_tokens=stored["cache_write_tokens"],
            total_tokens=stored["total_tokens"],
            latency_ms=latency_ms,
            request_id=request_id,
            user_id=user_id,
            error_code=error_code,
            artifact_bytes=artifact_bytes,
            tool_names=tools,
            tool_counts=tool_counts,
            mcp_names=mcp_names,
            mcp_counts=mcp_counts,
            failure=(
                UsageFailure.model_validate(failure_dict(failure))
                if failure is not None
                else None
            ),
        )
        try:
            self.outbox.append(
                session_id,
                "usage",
                usage_payload,
                turn_id=turn_id,
                emergency=self._emergency or status == "failed",
            )
        finally:
            self._tally.reset()
            self._turn_started.pop(turn_id, None)
        resolved_metrics = metrics if metrics is not None else self.metrics
        resolved_tracing = tracing if tracing is not None else self.tracing
        if failure is not None:
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
        if resolved_metrics is not None:
            from apipi.common.metrics import observe_turn

            observe_turn(
                resolved_metrics,
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
        if resolved_tracing is not None:
            from apipi.common.otel import set_span

            set_span(
                resolved_tracing,
                request_id=request_id,
                session_id=session_id,
                turn_id=turn_id,
                model=None,
                status=status,
                prompt_tokens=stored["prompt_tokens"],
                completion_tokens=stored["completion_tokens"],
                cache_read_tokens=stored["cache_read_tokens"],
                cache_write_tokens=stored["cache_write_tokens"],
                total_tokens=stored["total_tokens"],
                tool_names=tools,
            )

    async def store_input_image(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        data: bytes,
        filename: str,
        content_type: str | None,
        settings: Any | None = None,
    ) -> dict[str, Any]:
        from apipi.worker.artifact_upload import upload_via_presign

        self._check(tenant_id, session_id)
        resolved = settings if settings is not None else self.settings
        assert resolved is not None
        return await upload_via_presign(
            self.outbox,
            self.waiters,
            resolved,
            session_id,
            kind="input_image",
            filename=filename,
            content_type=content_type,
            data=data,
        )

    async def store_artifacts(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        files: list[tuple[str, bytes]],
        *,
        turn_id: uuid.UUID | None = None,
        settings: Any | None = None,
    ) -> None:
        from apipi.worker.artifact_upload import upload_via_presign
        from apipi.worker.pi.artifacts import _content_type

        if not files:
            return
        self._check(tenant_id, session_id)
        resolved = settings if settings is not None else self.settings
        assert resolved is not None
        for rel, data in files:
            await upload_via_presign(
                self.outbox,
                self.waiters,
                resolved,
                session_id,
                kind="artifact",
                filename=rel,
                content_type=_content_type(rel),
                data=data,
                turn_id=turn_id,
            )

    async def store_pi_session(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        data: bytes,
        *,
        settings: Any | None = None,
    ) -> None:
        from apipi.worker.artifact_upload import upload_via_presign

        if not data:
            return
        self._check(tenant_id, session_id)
        resolved = settings if settings is not None else self.settings
        assert resolved is not None
        await upload_via_presign(
            self.outbox,
            self.waiters,
            resolved,
            session_id,
            kind="pi_session",
            filename="pi-session.jsonl",
            content_type="application/octet-stream",
            data=data,
        )
