"""Protocol constants: version, close codes, message type sets, limits."""

from datetime import timedelta

PROTOCOL_VERSION = 2

WORKER_CLOSE_CODE = 1008
UNSUPPORTED_PROTOCOL_REASON = "unsupported_protocol"
UNAUTHORIZED_REASON = "unauthorized"
REVOKED_REASON = "revoked"
REGISTER_REQUIRED_REASON = "register_required"
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
        "event",
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
MAX_MESSAGE_BYTES = 1_000_000
MAX_COMMAND_BYTES = 256_000
DELTA_RATE_LIMIT = 100
DELTA_MAX_TEXT = 32_768
SEEN_INTERVAL = timedelta(seconds=5)
