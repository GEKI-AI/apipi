from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from apipi.common.errors import ApiError
from apipi.common.sandbox import sandbox_size_of
from apipi.protocol import (
    COMMAND_PAYLOAD_MODELS,
    CURSOR_OPS,
    BaseCommandPayload,
    CommandTooLarge,
    ContextBytes,
    check_command_size,
    parse_turn_context,
)

if TYPE_CHECKING:
    from apipi.workerhub.hub import WorkerHub


def session_image(environment: dict[str, Any] | None) -> str:
    from apipi.common.sandbox import (
        image_for_size,
        sandbox_image_of,
    )

    stored = sandbox_image_of(environment)
    if stored is not None:
        return stored
    return image_for_size(sandbox_size_of(environment))


def image_unavailable_message(hub: WorkerHub, image: str) -> str:
    from apipi.common.image_recipes import recipe_archs

    arches = {
        conn.arch
        for conn in hub._conns.values()
        if "microvm" in conn.accepts and conn.arch and image not in conn.images
    }
    supported = recipe_archs(image)
    if arches and supported and arches.isdisjoint(supported):
        listed = ", ".join(sorted(arches))
        return f'sandbox_image "{image}" is not built for {listed}'
    return f'No worker has sandbox_image "{image}". Run apipi images pull on a worker.'


def command_payload(
    op: str,
    payload: BaseCommandPayload | dict[str, Any] | None,
    *,
    run_mode: str | None,
    image: str | None,
    cursor: int | None = None,
) -> BaseCommandPayload:
    """The payload model of one command, with the placement fields filled in.

    `payload` is the caller's model or its fields as a dict. `image` is
    set only for microvm sessions, and `cursor` only on the ops that
    carry the session sequence cursor.
    """
    if isinstance(payload, BaseCommandPayload):
        model = payload
    else:
        model = COMMAND_PAYLOAD_MODELS[op].model_validate(payload or {})
    update: dict[str, Any] = {}
    if run_mode is not None:
        update["run_mode"] = run_mode
    if image is not None:
        update["sandbox_image"] = image
    if cursor is not None and op in CURSOR_OPS:
        update["last_seq"] = cursor
    return model.model_copy(update=update)


def _check_command_context(op: str, payload: dict[str, Any]) -> None:
    """Validate the turn context on worker commands before sending."""
    raw = payload.get("context")
    if raw is None:
        return
    try:
        parse_turn_context(raw)
    except (ContextBytes, ValidationError) as exc:
        raise ApiError(
            "invalid_request",
            f"invalid turn context: {exc}",
            code="invalid_request",
            status_code=400,
        ) from exc
    try:
        check_command_size(payload)
    except CommandTooLarge as exc:
        raise ApiError(
            "invalid_request",
            str(exc),
            code="payload_too_large",
            status_code=413,
        ) from exc
