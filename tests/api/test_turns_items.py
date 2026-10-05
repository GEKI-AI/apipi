from httpx import AsyncClient
from tests.support.http import auth, session_with_turn

from apipi.worker.fake_harness import FAKE_USAGE


async def test_turns_and_items_after_a_turn(client: AsyncClient) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)

    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    assert turns.status_code == 200
    data = turns.json()["data"]
    assert len(data) == 1
    assert data[0]["status"] == "completed"
    assert data[0]["session_id"] == session_id
    turn_id = data[0]["id"]

    one = await client.get(
        f"/v1/agents/sessions/{session_id}/turns/{turn_id}",
        headers=auth(token),
    )
    assert one.status_code == 200
    assert one.json()["id"] == turn_id
    assert one.json()["usage"] == FAKE_USAGE

    items = await client.get(
        f"/v1/agents/sessions/{session_id}/items", headers=auth(token)
    )
    assert items.status_code == 200
    types = [item["type"] for item in items.json()["data"]]
    assert types == ["message", "message"]
    roles = [item["data"]["role"] for item in items.json()["data"]]
    assert roles == ["user", "assistant"]
    assert items.json()["data"][0]["data"]["content"] == "hello"
    assert items.json()["data"][1]["data"]["content"] == "hello"
    assert items.json()["data"][0]["turn_id"] == turn_id


async def test_turn_from_other_session_is_404(client: AsyncClient) -> None:
    token = "t"
    session_a = await session_with_turn(client, token)
    session_b = await session_with_turn(client, token)
    turns_a = await client.get(
        f"/v1/agents/sessions/{session_a}/turns", headers=auth(token)
    )
    turn_id = turns_a.json()["data"][0]["id"]
    other = await client.get(
        f"/v1/agents/sessions/{session_b}/turns/{turn_id}",
        headers=auth(token),
    )
    assert other.status_code == 404
