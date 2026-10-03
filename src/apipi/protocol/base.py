"""Base classes for the worker protocol models, named by role.

The `extra` policy of each role is set here and nowhere else, so a
later change of the policy is one line per role.
"""

import contextlib
import contextvars
import functools
import typing
from collections.abc import Iterator
from typing import Any, ClassVar, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, model_validator

_strict = False
_unknown: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "protocol_unknown_fields", default=None
)


def set_strict_parse(enabled: bool) -> bool:
    """Make the strict models reject unknown fields; returns the old setting.

    Receivers ignore unknown fields so a newer peer still parses. Tests
    switch this on, so a typo in one of our own senders still fails.
    """
    global _strict
    previous = _strict
    _strict = enabled
    return previous


@contextlib.contextmanager
def strict_parse(enabled: bool = True) -> Iterator[None]:
    previous = set_strict_parse(enabled)
    try:
        yield
    finally:
        set_strict_parse(previous)


@contextlib.contextmanager
def collect_unknown_fields() -> Iterator[list[str]]:
    """Collect `Model.field` for each unknown field parsed inside the block."""
    found: list[str] = []
    token = _unknown.set(found)
    try:
        yield found
    finally:
        _unknown.reset(token)


@functools.cache
def _constant_fields(model: type[BaseModel]) -> tuple[str, ...]:
    names: list[str] = []
    for name, info in model.model_fields.items():
        if typing.get_origin(info.annotation) is Literal and (
            len(typing.get_args(info.annotation)) == 1
        ):
            names.append(name)
    return tuple(names)


@functools.cache
def _known_keys(model: type[BaseModel]) -> frozenset[str]:
    keys: set[str] = set()
    for name, info in model.model_fields.items():
        keys.add(name)
        for alias in (info.alias, info.validation_alias):
            if isinstance(alias, str):
                keys.add(alias)
            elif isinstance(alias, AliasChoices):
                keys.update(c for c in alias.choices if isinstance(c, str))
    return frozenset(keys)


class WireModel(BaseModel):
    """A model that serializes to the exact JSON object that goes on the wire.

    `to_wire` writes the fields the sender set. A field the sender left
    at its default is absent, and a field set to `None` is `null`.
    Fields with a single allowed value, like `type`, are always written.
    Unknown fields are ignored. Inside `collect_unknown_fields` they are
    listed, and with `strict_parse` a model with `strict_in_tests` set
    rejects them.
    """

    strict_in_tests: ClassVar[bool] = False

    @model_validator(mode="before")
    @classmethod
    def _note_unknown(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        sink = _unknown.get()
        if sink is None and not (_strict and cls.strict_in_tests):
            return data
        extra = [key for key in data if key not in _known_keys(cls)]
        if not extra:
            return data
        if sink is not None:
            sink.extend(f"{cls.__name__}.{key}" for key in extra)
        if _strict and cls.strict_in_tests:
            raise ValueError(f"unknown fields in {cls.__name__}: {', '.join(extra)}")
        return data

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
    """The payload of one envelope. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")
    strict_in_tests = True


class CommandPayload(WireModel):
    """The payload of one API to worker command. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")


class ContextPart(WireModel):
    """One section of the command context. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")
    strict_in_tests = True
