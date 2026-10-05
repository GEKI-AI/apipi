import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from tests.support.http import auth, session_with_turn

from apipi.worker.fake_harness import FAKE_USAGE

_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def _expected(turns: int) -> dict[str, int]:
    body = {key: FAKE_USAGE[key] * turns for key in _TOKEN_KEYS}
    body["turns"] = turns
    body["search_calls"] = 0
    body["search_units"] = 0
    return body


async def _turn_id(client: AsyncClient, token: str, session_id: str) -> str:
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=auth(token)
    )
    return str(turns.json()["data"][0]["id"])


@pytest.mark.parametrize("key", ["session_id", "turn_id", "day"])
async def test_usage_by_session_turn_and_day(client: AsyncClient, key: str) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)
    values = {
        "session_id": session_id,
        "turn_id": await _turn_id(client, token, session_id),
        "day": datetime.now(UTC).date().isoformat(),
    }
    response = await client.get(
        "/v1/apipi/usage", headers=auth(token), params={key: values[key]}
    )
    assert response.status_code == 200
    assert response.json() == _expected(1)
    assert "cost" not in response.json()
    assert "usd" not in response.json()
    assert "hello" not in str(response.json())


async def test_usage_by_session_sums_turns(client: AsyncClient) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    response = await client.get(
        "/v1/apipi/usage", headers=auth(token), params={"session_id": session_id}
    )
    assert response.status_code == 200
    assert response.json() == _expected(2)


@pytest.mark.parametrize(
    ("key", "status"), [("session_id", 404), ("turn_id", 404), ("day", 200)]
)
async def test_usage_other_tenant_and_unknown_ids(
    client: AsyncClient, key: str, status: int
) -> None:
    session_id = await session_with_turn(client, "a")
    owned = {
        "session_id": session_id,
        "turn_id": await _turn_id(client, "a", session_id),
        "day": datetime.now(UTC).date().isoformat(),
    }
    missing = {
        "session_id": str(uuid.uuid4()),
        "turn_id": str(uuid.uuid4()),
        "day": "2020-01-02",
    }
    for token, value in (("b", owned[key]), ("a", missing[key])):
        response = await client.get(
            "/v1/apipi/usage", headers=auth(token), params={key: value}
        )
        assert response.status_code == status
        if status == 404:
            assert response.json()["error"]["code"] == "not_found"
        else:
            assert response.json() == _expected(0)


async def test_usage_requires_one_filter(client: AsyncClient) -> None:
    token = "t"
    session_id = await session_with_turn(client, token)
    missing = await client.get("/v1/apipi/usage", headers=auth(token))
    both = await client.get(
        "/v1/apipi/usage",
        headers=auth(token),
        params={"session_id": session_id, "day": "2020-01-02"},
    )
    assert missing.status_code == 400
    assert both.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"
    assert both.json()["error"]["code"] == "invalid_request"
