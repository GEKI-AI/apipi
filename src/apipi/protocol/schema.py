"""JSON Schema of every worker protocol message, generated from the models.

`message_schemas` returns one schema document per file of
`docs/worker-protocol/schema/`, and `schema_name_for` picks the schema of
one frame. Both sides of a test can then check a frame without writing a
dict by hand. The files are written by `scripts/gen_worker_schema.py`.
"""

import re
from typing import Any

from apipi.protocol.base import WireModel
from apipi.protocol.commands import COMMAND_PAYLOAD_MODELS, WorkerCommand
from apipi.protocol.constants import PROTOCOL_VERSION
from apipi.protocol.context import TurnContext
from apipi.protocol.envelope import PAYLOAD_MODELS, WorkerEnvelope
from apipi.protocol.messages import API_MESSAGE_MODELS, WORKER_MESSAGE_MODELS

DIALECT = "https://json-schema.org/draft/2020-12/schema"
UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
TIMESTAMP_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
TIMESTAMP_FIELDS = frozenset({"started_at", "expires_at"})
CONTEXT_OPS = ("turn.start", "turn.continue", "sandbox.boot")

ENVELOPE_PREFIX = "envelope."
COMMAND_PREFIX = "command."


def _model_schema(model: type[WireModel]) -> dict[str, Any]:
    return model.model_json_schema(by_alias=True, mode="validation")


def _tighten(node: Any, key: str | None = None) -> None:
    """Make the schema as strict as the specification, not as lax as the parser."""
    if isinstance(node, dict):
        if node.get("format") == "uuid":
            node["pattern"] = UUID_PATTERN
        if key in TIMESTAMP_FIELDS:
            for option in node.get("anyOf", [node]):
                if option.get("type") == "string":
                    option["format"] = "date-time"
                    option["pattern"] = TIMESTAMP_PATTERN
        required = set(node.get("required", []))
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, spec in properties.items():
                if isinstance(spec, dict) and "const" in spec:
                    required.add(name)
            if required:
                node["required"] = sorted(required)
        for name, value in node.items():
            _tighten(value, name if key == "properties" else None)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)


def _document(schema: dict[str, Any], title: str) -> dict[str, Any]:
    document: dict[str, Any] = {"$schema": DIALECT, "title": title}
    document.update({k: v for k, v in schema.items() if k != "title"})
    _tighten(document)
    return document


def _inline(base: dict[str, Any], field: str, child: dict[str, Any]) -> dict[str, Any]:
    """Replace one property of `base` with the schema of `child`."""
    merged = dict(base)
    defs = dict(base.get("$defs", {}))
    child = dict(child)
    defs.update(child.pop("$defs", {}))
    child.pop("title", None)
    properties = dict(merged["properties"])
    properties[field] = child
    merged["properties"] = properties
    if defs:
        merged["$defs"] = defs
    return merged


def _require(schema: dict[str, Any], *names: str) -> dict[str, Any]:
    merged = dict(schema)
    merged["required"] = sorted({*schema.get("required", []), *names})
    return merged


def _const(base: dict[str, Any], field: str, value: str) -> dict[str, Any]:
    merged = dict(base)
    properties = dict(merged["properties"])
    properties[field] = {"const": value, "type": "string"}
    merged["properties"] = properties
    return merged


def message_schemas() -> dict[str, dict[str, Any]]:
    """Every schema, by file name without `.json`."""
    schemas: dict[str, dict[str, Any]] = {}
    for kind, model in {
        **WORKER_MESSAGE_MODELS,
        **API_MESSAGE_MODELS,
    }.items():
        if kind == "command":
            continue
        schemas[kind] = _document(_model_schema(model), f"{kind} message")
    base_command = _model_schema(WorkerCommand)
    schemas["command"] = _document(_require(base_command, "payload"), "command message")
    for op, payload_model in COMMAND_PAYLOAD_MODELS.items():
        payload = _model_schema(payload_model)
        if op in CONTEXT_OPS:
            context = _model_schema(TurnContext)
            payload = _inline(payload, "context", context)
        merged = _require(
            _inline(_const(base_command, "op", op), "payload", payload), "payload"
        )
        schemas[COMMAND_PREFIX + op] = _document(merged, f"command {op}")
    schemas["context"] = _document(_model_schema(TurnContext), "command context")
    base_envelope = _require(_model_schema(WorkerEnvelope), "payload")
    schemas["envelope"] = _document(base_envelope, "envelope")
    for kind, payload_model in PAYLOAD_MODELS.items():
        merged = _inline(
            _const(base_envelope, "type", kind), "payload", _model_schema(payload_model)
        )
        schemas[ENVELOPE_PREFIX + kind] = _document(merged, f"envelope {kind}")
    schemas["index"] = _index(schemas)
    return schemas


def _index(schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    def files(names: list[str]) -> dict[str, str]:
        return {name: f"{name}.json" for name in sorted(names)}

    worker_control = sorted(WORKER_MESSAGE_MODELS)
    api_control = sorted(set(API_MESSAGE_MODELS) - {"command"})
    return {
        "$schema": DIALECT,
        "title": "Worker protocol schema index",
        "protocol": PROTOCOL_VERSION,
        "worker_to_api": {
            "control": files(worker_control),
            "envelope": {
                kind: f"{ENVELOPE_PREFIX}{kind}.json" for kind in sorted(PAYLOAD_MODELS)
            },
        },
        "api_to_worker": {
            "control": files(api_control),
            "command": {
                op: f"{COMMAND_PREFIX}{op}.json"
                for op in sorted(COMMAND_PAYLOAD_MODELS)
            },
        },
        "context": "context.json",
        "files": sorted(name for name in schemas if name != "index"),
    }


WORKER_TO_API = "worker_to_api"
API_TO_WORKER = "api_to_worker"


def schema_name_for(direction: str, frame: dict[str, Any]) -> str | None:
    """The schema file (without `.json`) that describes one frame, or None.

    A frame with `v` is an envelope and is told apart by its `type`. A
    command is told apart by its `op`. Any other frame is a control
    message told apart by its `type`.
    """
    kind = frame.get("type")
    if not isinstance(kind, str):
        return None
    if direction == WORKER_TO_API:
        if "v" in frame:
            return ENVELOPE_PREFIX + kind if kind in PAYLOAD_MODELS else None
        return kind if kind in WORKER_MESSAGE_MODELS else None
    if kind == "command":
        op = frame.get("op")
        if isinstance(op, str) and op in COMMAND_PAYLOAD_MODELS:
            return COMMAND_PREFIX + op
        return None
    if kind in API_MESSAGE_MODELS:
        return kind
    return None


def file_name(name: str) -> str:
    if not re.fullmatch(r"[a-z.]+", name):
        raise ValueError(name)
    return f"{name}.json"
