"""API to worker commands: `{type: "command", id, session_id, lease_id, op, payload}`.

The payload has one model per `op`. Every payload carries the tenant
and the request attribution. `turn.start`, `turn.continue`, and
`sandbox.boot` also carry the command `context` and the `last_seq`
cursor the worker continues its session sequence from. The model key
travels only in `context.model.api_key`.
"""

import uuid
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field

from apipi.protocol.base import CommandPayload, ContextPart, ControlMessage
from apipi.protocol.context import TurnContext, parse_turn_context


class BaseCommandPayload(CommandPayload):
    """Fields every command carries.

    `tenant_id` is required on the wire: a worker drops a command whose
    tenant id is missing or invalid. `run_mode` is the placement kind
    the API chose, and `sandbox_image` the image of a microvm session.
    """

    tenant_id: uuid.UUID | None = None
    request_id: str | None = None
    key_id: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    traceparent: str | None = None
    run_mode: str | None = None
    sandbox_image: str | None = None


class ContextCommandPayload(BaseCommandPayload):
    """Fields of the commands that carry a context and the seq cursor."""

    context: dict[str, Any] | None = None
    last_seq: int | None = Field(default=None, ge=0)

    def turn_context(self) -> TurnContext | None:
        """The context as a model; raises when it is not a valid context."""
        if self.context is None:
            return None
        return parse_turn_context(self.context)


class InputTextPart(ContextPart):
    type: Literal["input_text"] = "input_text"
    text: str = ""


class InputImageRef(ContextPart):
    """One input image as a store reference, like a context file reference.

    `url` is a presigned GET URL (S3 store) and `local_path` a path
    relative to the shared store root (filesystem store). The bytes
    never travel in the command.
    """

    type: Literal["image"] = "image"
    file_id: str
    object_id: str
    url: str | None = None
    local_path: str | None = None
    mime_type: str
    size_bytes: int | None = Field(default=None, ge=0)


class InputFileRef(ContextPart):
    """One `input_file` of a session without a computer, as a store reference.

    `model_input` says how the worker passes it to the model: `text`
    (UTF-8 text in a `<file name="…">` block of the prompt) or `image`.
    The bytes never travel in the command.
    """

    type: Literal["file"] = "file"
    file_id: str
    filename: str
    object_id: str
    url: str | None = None
    local_path: str | None = None
    mime_type: str
    size_bytes: int | None = Field(default=None, ge=0)
    model_input: Literal["text", "image"]


TurnInputPart = Annotated[
    InputTextPart | InputImageRef | InputFileRef, Field(discriminator="type")
]


class TurnStartCommandPayload(ContextCommandPayload):
    """`images` is always empty; images and files travel in `parts` as references."""

    text: str | None = None
    images: list[dict[str, Any]] | None = None
    parts: list[TurnInputPart] | None = None


class TurnContinueCommandPayload(ContextCommandPayload):
    turn_id: uuid.UUID | None = None
    call_id: str | None = None
    success: bool | None = None
    output: str | None = None
    error: str | None = None


class TurnCancelCommandPayload(BaseCommandPayload):
    pass


class SessionStopCommandPayload(BaseCommandPayload):
    pass


class SandboxBootCommandPayload(ContextCommandPayload):
    pass


COMMAND_PAYLOAD_MODELS: dict[str, type[BaseCommandPayload]] = {
    "turn.start": TurnStartCommandPayload,
    "turn.continue": TurnContinueCommandPayload,
    "turn.cancel": TurnCancelCommandPayload,
    "session.stop": SessionStopCommandPayload,
    "sandbox.boot": SandboxBootCommandPayload,
}


class WorkerCommand(ControlMessage):
    """API to worker. `command_id` travels on the wire as `id`."""

    type: Literal["command"] = "command"
    command_id: uuid.UUID = Field(validation_alias="id", serialization_alias="id")
    session_id: uuid.UUID
    lease_id: uuid.UUID
    op: str
    payload: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)

    @classmethod
    def build(
        cls,
        command_id: uuid.UUID,
        session_id: uuid.UUID,
        lease_id: uuid.UUID,
        op: str,
        payload: BaseCommandPayload,
    ) -> "WorkerCommand":
        """Wrap one payload model of the model the op names."""
        return cls(
            command_id=command_id,
            session_id=session_id,
            lease_id=lease_id,
            op=op,
            payload=payload.to_wire(),
        )

    def parsed_payload(self) -> BaseCommandPayload:
        """The payload as the model of its op.

        An unknown op parses as the base payload, so the worker can
        still read the tenant and ignore the command.
        """
        model = COMMAND_PAYLOAD_MODELS.get(self.op, BaseCommandPayload)
        return model.model_validate(self.payload)
