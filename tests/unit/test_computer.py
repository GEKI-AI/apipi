import uuid

from apipi.env.computer import (
    computer_item_events,
    run_computer_tool,
    tool_payload,
)
from apipi.env.hub import EnvironmentHub
from apipi.services.runtime import _cwd_and_tools


def test_tool_payload_maps_pi_names() -> None:
    assert tool_payload("bash", {"command": "ls"}) == ("exec", {"command": "ls"})
    assert tool_payload("read", {"path": "a.txt"}) == ("read", {"path": "a.txt"})
    assert tool_payload("write", {"path": "a.txt", "content": "hi"}) == (
        "write",
        {"path": "a.txt", "content": "hi"},
    )
    assert tool_payload("edit", {"path": "a.txt", "oldText": "a", "newText": "b"}) == (
        "edit",
        {"path": "a.txt", "old_text": "a", "new_text": "b"},
    )
    assert tool_payload("unknown", {}) is None


def test_computer_item_events_are_command_execution() -> None:
    events = computer_item_events("c1", "bash", is_error=False)
    assert events[0][0] == "agent.session.turn.item.added"
    assert events[0][1]["item_type"] == "command_execution"
    assert events[1][1]["is_error"] is False


def test_cwd_and_tools_self_hosted_off_until_connected() -> None:
    env = {"type": "self_hosted", "id": str(uuid.uuid4())}
    cwd, tools, env_id = _cwd_and_tools(env)
    assert cwd is None
    assert tools is False
    assert env_id is None
    cwd, tools, env_id = _cwd_and_tools(env, EnvironmentHub())
    assert tools is False
    assert env_id is None


async def test_run_computer_tool_disconnected() -> None:
    hub = EnvironmentHub()
    result = await run_computer_tool(hub, uuid.uuid4(), "bash", {"command": "ls"})
    assert result == {"ok": False, "error": "disconnected"}
