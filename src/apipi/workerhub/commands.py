from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from apipi.common.errors import ApiError
from apipi.common.sandbox import sandbox_size_of
from apipi.protocol import (
    COMMAND_PAYLOAD_MODELS,
    CURSOR_OPS,
    FEATURE_FILE_REFS,
    FEATURE_IMAGE_REFS,
    OP_FEATURES,
    BaseCommandPayload,
    CommandTooLarge,
    ContextBytes,
    check_command_size,
    parse_turn_context,
)


def session_image(environment: dict[str, Any] | None) -> str:
    from apipi.common.sandbox import (
        image_for_size,
        sandbox_image_of,
    )

    stored = sandbox_image_of(environment)
    if stored is not None:
        return stored
    return image_for_size(sandbox_size_of(environment))


def image_unavailable_message(arches: set[str], image: str) -> str:
    from apipi.common.image_recipes import recipe_archs

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


def command_features(wire: dict[str, Any]) -> list[str]:
    """The protocol features a worker must list to receive this command."""
    op = str(wire.get("op"))
    needed: list[str] = []
    feature = OP_FEATURES.get(op)
    if feature is not None:
        needed.append(feature)
    payload = wire.get("payload")
    parts = payload.get("parts") if isinstance(payload, dict) else None
    kinds = {
        item.get("type")
        for item in (parts if isinstance(parts, list) else [])
        if isinstance(item, dict)
    }
    if op == "turn.start" and "image" in kinds:
        needed.append(FEATURE_IMAGE_REFS)
    if op == "turn.start" and "file" in kinds:
        needed.append(FEATURE_FILE_REFS)
    return needed


def _too_large_message(op: str, exc: CommandTooLarge) -> str:
    if op == "turn.start":
        return (
            f"The message and the session context need {exc.size} bytes, more "
            f"than the {exc.limit} bytes one turn can carry. Shorten the message, "
            "the instructions, or the tool definitions."
        )
    return (
        f"The session context needs {exc.size} bytes, more than the "
        f"{exc.limit} bytes one worker command can carry."
    )


def _check_command_context(op: str, wire: dict[str, Any]) -> None:
    """Validate the turn context and the byte size of a command before sending."""
    payload = wire["payload"]
    raw = payload.get("context")
    if raw is not None:
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
        check_command_size(wire)
    except CommandTooLarge as exc:
        raise ApiError(
            "invalid_request",
            _too_large_message(op, exc),
            code="payload_too_large",
            status_code=413,
        ) from exc
