from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: list[str] = Field(default_factory=lambda: ["text"])
    reasoning: bool | None = None
    thinking_levels: dict[str, str | None] | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    compat: dict[str, Any] | None = None


def registry_of(raw: dict[str, Any] | None) -> dict[str, ModelCapability]:
    out: dict[str, ModelCapability] = {}
    if not raw:
        return out
    for model_id, value in raw.items():
        if isinstance(value, ModelCapability):
            out[model_id] = value
        elif isinstance(value, dict):
            out[model_id] = ModelCapability.model_validate(value)
    return out


def model_accepts_image(raw: dict[str, Any] | None, model: str | None) -> bool:
    if not isinstance(model, str) or not model:
        return False
    caps = registry_of(raw).get(model)
    if caps is None:
        return False
    return "image" in caps.input


def apply_capability(
    row: dict[str, object],
    caps: ModelCapability | None,
    *,
    reasoning: bool,
) -> None:
    if caps is None:
        if reasoning:
            row["reasoning"] = True
        return
    if caps.input:
        row["input"] = list(caps.input)
    if caps.reasoning is True or (caps.reasoning is None and reasoning):
        row["reasoning"] = True
    elif "reasoning" in row and not (caps.reasoning is True or reasoning):
        row.pop("reasoning", None)
    if caps.thinking_levels:
        row["thinkingLevelMap"] = dict(caps.thinking_levels)
    if caps.context_window is not None:
        row["contextWindow"] = caps.context_window
    if caps.max_tokens is not None:
        row["maxTokens"] = caps.max_tokens
    compat = dict(caps.compat or {})
    if caps.reasoning is True or (caps.reasoning is None and reasoning):
        compat["supportsReasoningEffort"] = True
    if compat:
        row["compat"] = compat
