from httpx import AsyncClient
from tests.support.http import auth, session_with_turn

from apipi.protocol import PUBLIC_EVENT_TYPES


async def test_export_after_a_turn(client: AsyncClient) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)
    exported = await client.get(
        f"/v1/apipi/sessions/{session_id}/export", headers=auth(token)
    )
    assert exported.status_code == 200
    body = exported.json()
    types = [event["type"] for event in body["events"]]
    assert types[0] == "agent.session.created"
    assert types[-1] == "agent.session.idle"
    assert set(types) <= PUBLIC_EVENT_TYPES
    assert "agent.session.turn.output_text.delta" not in types
    done = [
        event
        for event in body["events"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello"
    assert len(body["turns"]) == 1
    assert body["turns"][0]["status"] == "completed"
    assert body["turns"][0]["session_id"] == session_id
    roles = [item["data"]["role"] for item in body["items"]]
    assert roles == ["user", "assistant"]
    assert body["items"][0]["data"]["content"] == "hello"
    assert body["items"][1]["data"]["content"] == "hello"
    events = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=auth(token)
    )
    assert body["events"] == events.json()["data"]
    assert body["turns"] == turns.json()["data"]
    assert body["items"] == items.json()["data"]
