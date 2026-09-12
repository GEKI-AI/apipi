import asyncio
import os
from typing import Any

from openai import AsyncOpenAI


def _json(response: object) -> dict[str, Any]:
    status = getattr(response, "status_code", None)
    payload = getattr(response, "http_response").json()
    if status != 200:
        raise RuntimeError(f"request failed: {status} {payload}")
    if not isinstance(payload, dict):
        raise TypeError("expected a JSON object")
    return payload


async def create_agent_and_read_turn(client: AsyncOpenAI) -> dict[str, Any]:
    agents = client.beta.agents
    agent = _json(
        await agents.with_raw_response.create(
            model="gpt-4.1",
            name="demo",
            instructions="Be brief.",
        )
    )
    session = _json(
        await agents.sessions.with_raw_response.create(
            environment={"type": "none"},
            agent_id=agent["id"],
            input="Hello",
        )
    )
    listed = _json(await agents.sessions.turns.with_raw_response.list(session["id"]))
    turns = listed.get("data")
    if not isinstance(turns, list) or not turns:
        raise RuntimeError("session has no turns")
    turn = turns[0]
    if not isinstance(turn, dict):
        raise RuntimeError("turn is not an object")
    return {"agent": agent, "session": session, "turn": turn}


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY to a bearer the gateway will accept.")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
        max_retries=0,
    )
    result = await create_agent_and_read_turn(client)
    print(result["session"]["id"])
    print(result["turn"]["id"], result["turn"]["status"])


if __name__ == "__main__":
    asyncio.run(main())
