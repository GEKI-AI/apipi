import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from tests.support.procs import fake_pi_shim
from tests.support.split_worker import split_client_for, worker_settings_for

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.worker.pi.harness import PiHarness


@dataclass
class _Run:
    statuses: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    hooked: list[bool] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    sandbox: list[str] = field(default_factory=list)
    leased: bool = False


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _message(text: str) -> dict[str, Any]:
    content = [{"type": "input_text", "text": text}]
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": content}],
            }
        ]
    }


async def _send(client: AsyncClient, token: str, session_id: str, text: str) -> str:
    response = await asyncio.wait_for(
        client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json=_message(text),
        ),
        timeout=15,
    )
    assert response.status_code == 200, response.json()
    return str(response.json()["status"])


def _reasons(sent: list[str], kind: str) -> list[str]:
    reasons: list[str] = []
    for raw in sent:
        message = json.loads(raw)
        payload = message.get("payload") or {}
        if message.get("type") == kind and payload.get("reason"):
            reasons.append(payload["reason"])
    return reasons


async def _two_turns(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    between: str,
    environment: str,
) -> _Run:
    run = _Run()
    sent: list[str] = []
    token = f"split-{between}-{environment}"
    worker_settings = worker_settings_for(
        settings.model_copy(update={"pi_command": str(fake_pi_shim(tmp_path))})
    )
    async with split_client_for(
        settings,
        store,
        token=worker_secret,
        worker_settings=worker_settings,
        sent=sent,
    ) as (_app, client, worker):
        execution = worker.execution
        execution.harness = PiHarness(execution.pool)
        pool = execution.pool
        real_kill = pool.kill
        real_hook = pool.on_kill
        assert real_hook is not None

        async def kill(session: Any, **kw: Any) -> None:
            run.killed.append(kw.get("reason", "session"))
            await real_kill(session, **kw)

        async def on_kill(session: uuid.UUID, proc: Any, release: bool) -> None:
            run.hooked.append(release)
            await real_hook(session, proc, release)

        async def sweep_dead() -> None:
            return None

        pool.kill = kill
        pool.on_kill = on_kill
        pool.sweep_dead = sweep_dead
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test", "instructions": "one"},
        )
        assert agent.status_code == 200, agent.json()
        agent_id = agent.json()["id"]
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": environment}},
        )
        assert created.status_code == 200, created.json()
        session_id = str(created.json()["id"])
        run.statuses.append(await _send(client, token, session_id, "first"))
        if between == "respawn":
            updated = await client.post(
                f"/v1/agents/{agent_id}",
                headers=_auth(token),
                json={"instructions": "two"},
            )
            assert updated.status_code == 200, updated.json()
        else:
            proc = pool.peek(uuid.UUID(session_id))
            assert proc is not None
            await proc.terminate()
        run.statuses.append(await _send(client, token, session_id, "second"))
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events?limit=100", headers=_auth(token)
        )
        run.types = [event["type"] for event in events.json()["data"]]
        run.leased = uuid.UUID(session_id) in worker.session_leases
    run.stops = _reasons(sent, "lifecycle.stop")
    run.sandbox = _reasons(sent, "sandbox.status")
    return run


@pytest.mark.parametrize("environment", ["none", "openai_hosted"])
@pytest.mark.parametrize("between", ["respawn", "crash"])
async def test_a_new_pi_at_turn_start_keeps_the_lease(
    settings: Settings,
    store: Store,
    worker_secret: str,
    tmp_path: Path,
    between: str,
    environment: str,
) -> None:
    run = await _two_turns(
        settings, store, worker_secret, tmp_path, between, environment
    )
    assert run.statuses == ["idle", "idle"], run.types
    assert run.types.count("agent.session.turn.completed") == 2, run.types
    assert "agent.session.error" not in run.types
    assert run.killed[0] == between
    assert run.hooked[0] is False
    assert run.stops[0] == between
    assert run.leased
    if environment == "openai_hosted":
        assert run.sandbox[0] == between
