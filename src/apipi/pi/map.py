from typing import Any


def _tool_item_type(name: object) -> str:
    if isinstance(name, str) and name.lower().startswith("mcp"):
        return "mcp_call"
    return "command_execution"


def map_pi_event(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    kind = event.get("type")
    if kind == "message_update":
        delta = event.get("assistantMessageEvent")
        if not isinstance(delta, dict):
            return []
        inner = delta.get("type")
        if inner == "text_delta":
            text = delta.get("delta")
            if not isinstance(text, str):
                text = ""
            return [("agent.session.turn.output_text.delta", {"delta": text})]
        if inner == "text_end":
            text = delta.get("content")
            if not isinstance(text, str):
                text = ""
            return [("agent.session.turn.output_text.done", {"text": text})]
        return []
    if kind == "tool_execution_start":
        call_id = event.get("toolCallId")
        name = event.get("toolName")
        item_type = _tool_item_type(name)
        return [
            (
                "agent.session.turn.item.added",
                {
                    "item_type": item_type,
                    "call_id": call_id,
                    "name": name,
                },
            )
        ]
    if kind == "tool_execution_end":
        call_id = event.get("toolCallId")
        name = event.get("toolName")
        return [
            (
                "agent.session.turn.item.done",
                {
                    "item_type": _tool_item_type(name),
                    "call_id": call_id,
                    "is_error": bool(event.get("isError")),
                },
            )
        ]
    return []
