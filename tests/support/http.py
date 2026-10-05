import json
import uuid
from typing import Any

import httpx
from httpx import AsyncClient

from apipi.gateway.tokens import hash_token

_OriginalClient = httpx.AsyncClient


def auth(token: str, user: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if user is not None:
        headers["X-End-User"] = user
    return headers


def tenant_of(token: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, hash_token(token))


def parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        data = None
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = line[6:]
        if data is not None:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            events.append(parsed)
    return events


async def create_agent(client: AsyncClient, token: str, **fields: Any) -> str:
    response = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={"name": "bot", "model": "test", **fields},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


async def session_with_turn(client: AsyncClient, token: str) -> str:
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": await create_agent(client, token),
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200
    return str(created.json()["id"])


async def post_message(
    client: AsyncClient, token: str, session_id: object, text: str
) -> httpx.Response:
    return await client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": text},
    )


class MockClient:
    def __init__(self, transport: httpx.MockTransport) -> None:
        self._client = _OriginalClient(transport=transport)

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()


def read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)
