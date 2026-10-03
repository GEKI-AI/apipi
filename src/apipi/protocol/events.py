"""The public session events a worker may report."""

PUBLIC_EVENT_TYPES = frozenset(
    {
        "agent.session.created",
        "agent.session.in_progress",
        "agent.session.idle",
        "agent.session.requires_action",
        "agent.session.failed",
        "agent.session.error",
        "agent.session.turn.created",
        "agent.session.turn.in_progress",
        "agent.session.turn.completed",
        "agent.session.turn.failed",
        "agent.session.turn.cancelled",
        "agent.session.turn.output_text.delta",
        "agent.session.turn.output_text.done",
        "agent.session.turn.item.added",
        "agent.session.turn.item.done",
        "agent.session.turn.item.nested",
        "agent.session.turn.thinking.started",
        "agent.session.turn.thinking.completed",
        "agent.session.turn.compaction.started",
        "agent.session.turn.compaction.completed",
        "agent.session.turn.retrying",
        "agent.session.turn.retry.completed",
        "agent.session.environment.pending",
        "agent.session.environment.connected",
        "agent.session.environment.disconnected",
        "agent.session.environment.failed",
    }
)

LIVE_EVENT_TYPES = frozenset({"agent.session.turn.output_text.delta"})
