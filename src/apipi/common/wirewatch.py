"""Count and log what a receiver ignores on the worker socket.

A receiver ignores unknown fields, so a newer peer still parses. It
never ignores them silently: each one is counted in
`apipi_worker_protocol_total{event="unknown_field"}` and logged as
`worker.protocol.unknown_field`, rate limited per field. Unknown message
types and command ops are counted as `unknown_type` and `unknown_op`.
"""

import logging
from typing import Any

from apipi.common.logutil import RateLimitedLog

log = logging.getLogger("apipi.worker")

_warnings = RateLimitedLog(log)


def note_unknown_fields(found: list[str], *, metrics: Any | None, side: str) -> None:
    """Count and log the `Model.field` names a parse ignored."""
    by_model: dict[str, list[str]] = {}
    for name in dict.fromkeys(found):
        model, _, field = name.partition(".")
        by_model.setdefault(model, []).append(field[:64])
        if metrics is not None:
            metrics.observe_worker_protocol("unknown_field")
    for model, fields in by_model.items():
        _warnings.warning(
            "worker message has unknown fields; ignored",
            event="worker.protocol.unknown_field",
            error_code="unknown_field",
            key=f"worker.protocol.unknown_field:{model}",
            model=model,
            fields=fields[:8],
            side=side,
        )


def note_unknown_type(
    kind: str, name: object, *, metrics: Any | None, side: str
) -> None:
    """Count and log one message type or command op this side does not know.

    `kind` is `type` or `op`. The name is logged, never used as a label.
    """
    event = f"unknown_{kind}"
    if metrics is not None:
        metrics.observe_worker_protocol(event)
    _warnings.warning(
        f"worker message has an unknown {kind}; not handled",
        event=f"worker.protocol.{event}",
        error_code=event,
        key=f"worker.protocol.{event}",
        side=side,
        peer_name=str(name)[:64],
    )
