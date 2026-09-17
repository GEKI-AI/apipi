import uuid
from datetime import UTC, datetime

from httpx import AsyncClient

from apipi.services.runtime import FAKE_USAGE

_TOKEN_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)


def _token(name: str = "t") -> str:
    return name


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _expected(turns: int) -> dict[str, int]:
    body = {key: FAKE_USAGE[key] * turns for key in _TOKEN_KEYS}
    body["turns"] = turns
    return body


async def _session_with_turn(client: AsyncClient, token: str) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def _turn_id(client: AsyncClient, token: str, session_id: str) -> str:
    turns = await client.get(
        f"/v1/agents/sessions/{session_id}/turns", headers=_auth(token)
    )
    return str(turns.json()["data"][0]["id"])


async def test_usage_by_session_and_turn(client: AsyncClient) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    turn_id = await _turn_id(client, token, session_id)
    session_usage = await client.get(
        "/v1/usage", headers=_auth(token), params={"session_id": session_id}
    )
    turn_usage = await client.get(
        "/v1/usage", headers=_auth(token), params={"turn_id": turn_id}
    )
    assert session_usage.status_code == 200
    assert turn_usage.status_code == 200
    assert session_usage.json() == _expected(1)
    assert turn_usage.json() == _expected(1)
    assert "cost" not in session_usage.json()
    assert "usd" not in session_usage.json()
    assert "hello" not in str(session_usage.json())


async def test_usage_by_session_sums_turns(client: AsyncClient) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    follow = await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert follow.status_code == 200
    response = await client.get(
        "/v1/usage", headers=_auth(token), params={"session_id": session_id}
    )
    assert response.status_code == 200
    assert response.json() == _expected(2)


async def test_usage_by_day(client: AsyncClient) -> None:
    token = _token()
    await _session_with_turn(client, token)
    day = datetime.now(UTC).date().isoformat()
    response = await client.get("/v1/usage", headers=_auth(token), params={"day": day})
    assert response.status_code == 200
    assert response.json() == _expected(1)


async def test_usage_empty_day_is_zeros(client: AsyncClient) -> None:
    token = _token()
    await client.get("/v1/agents", headers=_auth(token))
    response = await client.get(
        "/v1/usage", headers=_auth(token), params={"day": "2020-01-02"}
    )
    assert response.status_code == 200
    assert response.json() == _expected(0)


async def test_usage_day_is_tenant_scoped(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
    await _session_with_turn(client, token_a)
    day = datetime.now(UTC).date().isoformat()
    other = await client.get("/v1/usage", headers=_auth(token_b), params={"day": day})
    assert other.status_code == 200
    assert other.json() == _expected(0)


async def test_usage_wrong_tenant_is_404(client: AsyncClient) -> None:
    token_a = _token("a")
    token_b = _token("b")
    session_id = await _session_with_turn(client, token_a)
    turn_id = await _turn_id(client, token_a, session_id)
    session = await client.get(
        "/v1/usage", headers=_auth(token_b), params={"session_id": session_id}
    )
    turn = await client.get(
        "/v1/usage", headers=_auth(token_b), params={"turn_id": turn_id}
    )
    assert session.status_code == 404
    assert turn.status_code == 404
    assert session.json()["error"]["code"] == "not_found"
    assert turn.json()["error"]["code"] == "not_found"


async def test_usage_unknown_ids_are_404(client: AsyncClient) -> None:
    token = _token()
    missing = str(uuid.uuid4())
    session = await client.get(
        "/v1/usage", headers=_auth(token), params={"session_id": missing}
    )
    turn = await client.get(
        "/v1/usage", headers=_auth(token), params={"turn_id": missing}
    )
    assert session.status_code == 404
    assert turn.status_code == 404


async def test_usage_requires_one_filter(client: AsyncClient) -> None:
    token = _token()
    session_id = await _session_with_turn(client, token)
    missing = await client.get("/v1/usage", headers=_auth(token))
    both = await client.get(
        "/v1/usage",
        headers=_auth(token),
        params={"session_id": session_id, "day": "2020-01-02"},
    )
    assert missing.status_code == 400
    assert both.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_request"
    assert both.json()["error"]["code"] == "invalid_request"
