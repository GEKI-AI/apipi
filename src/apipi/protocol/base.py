"""Base classes for the worker protocol models, named by role.

The `extra` policy of each role is set here and nowhere else, so a
later change of the policy is one line per role.
"""

import functools
import typing
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


@functools.cache
def _constant_fields(model: type[BaseModel]) -> tuple[str, ...]:
    names: list[str] = []
    for name, info in model.model_fields.items():
        if typing.get_origin(info.annotation) is Literal and (
            len(typing.get_args(info.annotation)) == 1
        ):
            names.append(name)
    return tuple(names)


class WireModel(BaseModel):
    """A model that serializes to the exact JSON object that goes on the wire.

    `to_wire` writes the fields the sender set. A field the sender left
    at its default is absent, and a field set to `None` is `null`.
    Fields with a single allowed value, like `type`, are always written.
    """

    def model_post_init(self, __context: Any) -> None:
        self.__pydantic_fields_set__.update(_constant_fields(type(self)))

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_unset=True)


class ControlMessage(WireModel):
    """A message outside the envelope stream: handshake, replies, commands.

    Unknown fields are ignored so a newer peer still parses.
    """

    model_config = ConfigDict(extra="ignore")


class EnvelopePayload(WireModel):
    """The payload of one envelope. Unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


class CommandPayload(WireModel):
    """The payload of one API to worker command. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class ContextPart(WireModel):
    """One section of the command context. Unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")
