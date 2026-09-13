import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from apipi.env.hub import EnvDisconnected, EnvironmentHub

Computer = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

TOOL_VERBS = {
    "bash": "exec",
    "read": "read",
    "write": "write",
    "edit": "edit",
}


def _text(arguments: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = arguments.get(key)
        if isinstance(value, str):
            return value
    return ""


def tool_payload(
    name: str, arguments: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    verb = TOOL_VERBS.get(name)
    if verb is None:
        return None
    if verb == "exec":
        return verb, {"command": _text(arguments, "command")}
    if verb == "read":
        return verb, {"path": _text(arguments, "path")}
    if verb == "write":
        return verb, {
            "path": _text(arguments, "path"),
            "content": _text(arguments, "content"),
        }
    return verb, {
        "path": _text(arguments, "path"),
        "old_text": _text(arguments, "old_text", "oldText"),
        "new_text": _text(arguments, "new_text", "newText"),
    }


async def run_computer_tool(
    hub: EnvironmentHub,
    env_id: uuid.UUID,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    mapped = tool_payload(name, arguments)
    if mapped is None:
        return {"ok": False, "error": f"unknown tool {name}"}
    verb, payload = mapped
    try:
        return await hub.call(env_id, verb, **payload)
    except (EnvDisconnected, TimeoutError):
        return {"ok": False, "error": "disconnected"}


def bind_computer(hub: EnvironmentHub, env_id: uuid.UUID) -> Computer:
    async def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await run_computer_tool(hub, env_id, name, arguments)

    return _call


def computer_item_events(
    call_id: str, name: str, *, is_error: bool
) -> list[tuple[str, dict[str, Any]]]:
    payload = {
        "item_type": "command_execution",
        "call_id": call_id,
        "name": name,
    }
    return [
        ("agent.session.turn.item.added", dict(payload)),
        (
            "agent.session.turn.item.done",
            {**payload, "is_error": is_error},
        ),
    ]
