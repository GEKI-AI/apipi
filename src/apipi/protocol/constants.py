"""Protocol constants: version, close codes, message type sets, limits."""

import json
from collections.abc import Iterable
from datetime import timedelta

PROTOCOL_VERSION = 2

WORKER_CLOSE_CODE = 1008
UNSUPPORTED_PROTOCOL_REASON = "unsupported_protocol"
UNAUTHORIZED_REASON = "unauthorized"
REVOKED_REASON = "revoked"
REGISTER_REQUIRED_REASON = "register_required"
REGISTER_TIMEOUT_REASON = "register_timeout"
INVALID_REGISTER_REASON = "invalid_register"
TOKEN_BOUND_REASON = "token_bound"
SHARED_STORE_REASON = "shared_store_required"

DURABLE_MESSAGE_TYPES = frozenset(
    {
        "item.added",
        "item.done",
        "turn.status",
        "usage",
        "event",
        "session.status",
        "artifact.presign",
        "session.stopped",
        "workspace.reaped",
        "lifecycle.start",
        "lifecycle.stop",
        "artifact.completed",
        "error",
        "sandbox.status",
    }
)
EPHEMERAL_MESSAGE_TYPES = frozenset({"delta.text", "delta.reasoning"})
WORKER_MESSAGE_TYPES = DURABLE_MESSAGE_TYPES | EPHEMERAL_MESSAGE_TYPES

WORKER_IN = frozenset(
    {
        "register",
        "heartbeat",
        "lease.ack",
        "lease.release",
        "store.proof",
        "inventory",
        "sandbox.seen",
        "search.request",
    }
)
WORKER_OUT = frozenset(
    {
        "hello",
        "command",
        "ack",
        "artifact.presign.reply",
        "search.reply",
        "inventory.reply",
        "lease.revoke",
    }
)

COMMAND_OPS = frozenset(
    {"turn.start", "turn.cancel", "turn.continue", "session.stop", "sandbox.boot"}
)
COMMAND_CONTEXT_OPS = frozenset({"turn.start", "turn.continue", "sandbox.boot"})
CURSOR_OPS = frozenset({"turn.start", "turn.continue", "sandbox.boot"})

OUTBOX_BOUND = 10_000
MAX_MESSAGE_BYTES = 1_048_576
MAX_COMMAND_BYTES = 262_144
DELTA_RATE_LIMIT = 100
DELTA_MAX_TEXT = 32_768
SEEN_INTERVAL = timedelta(seconds=5)

FEATURE_SEARCH = "search"
FEATURE_PRESIGN = "presign"
FEATURE_LEASE_CURSOR = "lease_cursor"
FEATURE_SESSION_STOPPED = "session_stopped"
FEATURE_IMAGE_REFS = "image_refs"
FEATURE_FILE_REFS = "file_refs"
FEATURE_SESSION_FILES = "session_files"
FEATURE_ENV_CREDENTIALS = "env_credentials"

BASELINE_FEATURES = frozenset({FEATURE_SEARCH, FEATURE_PRESIGN, FEATURE_LEASE_CURSOR})
SUPPORTED_FEATURES = BASELINE_FEATURES | {
    FEATURE_SESSION_STOPPED,
    FEATURE_IMAGE_REFS,
    FEATURE_FILE_REFS,
    FEATURE_SESSION_FILES,
    FEATURE_ENV_CREDENTIALS,
}

TYPE_FEATURES: dict[str, str] = {
    "search.request": FEATURE_SEARCH,
    "search.reply": FEATURE_SEARCH,
    "artifact.presign": FEATURE_PRESIGN,
    "artifact.presign.reply": FEATURE_PRESIGN,
    "artifact.completed": FEATURE_PRESIGN,
}
OP_FEATURES: dict[str, str] = {}

KNOWN_WIRE_TYPES = WORKER_IN | WORKER_OUT | WORKER_MESSAGE_TYPES


def peer_features(advertised: Iterable[str] | None) -> frozenset[str]:
    """The features of a peer; no `features` field means the baseline set."""
    if advertised is None:
        return BASELINE_FEATURES
    return frozenset(item for item in advertised if isinstance(item, str))


def dumps_wire(message: object) -> str:
    """The JSON text of one frame: compact, UTF-8 (not ASCII-escaped).

    A string with a lone surrogate cannot be written as UTF-8, so such a
    message is escaped as ASCII instead and still goes on the wire.
    """
    text = json.dumps(message, separators=(",", ":"), ensure_ascii=False, default=str)
    if not text.isascii():
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return json.dumps(message, separators=(",", ":"), default=str)
    return text


def wire_bytes(text: str) -> int:
    """The size of one frame in bytes of UTF-8, the unit of every size limit."""
    return len(text) if text.isascii() else len(text.encode("utf-8"))


def wire_size(message: object) -> int:
    return wire_bytes(dumps_wire(message))


def wire_type(message: object) -> str:
    """The `type` of one socket message for a metric label, or `unknown`.

    Only the fixed message and envelope types are returned, so a peer
    cannot grow the label set.
    """
    if isinstance(message, dict):
        kind = message.get("type")
        if isinstance(kind, str) and kind in KNOWN_WIRE_TYPES:
            return kind
    return "unknown"
