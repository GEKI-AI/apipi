import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import tenant_from_key
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FAKE_USAGE, FakeHarness
from apipi.services.sidekick import SidekickError, sidekick_complete
from apipi.services.usage import usage_from
from apipi.store.engine import Store


class ThinkingHarness(FakeHarness):
    async def generate(
        self,
        text: str,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        yield (
            "thinking_body",
            {"item_id": "think-1", "text": "hidden thinking text"},
        )
        yield (
            "agent.session.turn.thinking.completed",
            {
                "item_id": "think-1",
                "content_index": 0,
                "duration_ms": 5,
                "reasoning_tokens": 2,
                "preview": "hidden",
                "preview_truncated": True,
            },
        )
        reply = text or "ok"
        yield ("agent.session.turn.output_text.delta", {"delta": reply})
        yield ("agent.session.turn.output_text.done", {"text": reply})
        yield ("usage", usage_from(FAKE_USAGE))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _authenticate(token: str) -> dict[str, object]:
    return {
        "key_id": hash_token(token),
        "tenant_id": tenant_from_key(token),
        "thinking_summary": token == "org-on",
    }


def _settings(tmp_path, *, summary: bool) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        thinking_summary=summary,
        sidekick_model="sidekick" if summary else None,
        sidekick_api_key="sidekick-key",
    )


async def _wait_idle(client: AsyncClient, token: str, session_id: str) -> list[dict]:
    events: list[dict] = []
    for _ in range(50):
        response = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
        )
        assert response.status_code == 200
        events = response.json()["data"]
        types = [event["type"] for event in events]
        if "agent.session.idle" in types and (
            "agent.session.turn.thinking.summary.completed" in types
            or "agent.session.turn.thinking.summary.failed" in types
        ):
            return events
        await asyncio.sleep(0.02)
    return events


async def _create(client: AsyncClient, token: str) -> tuple[int, str, str]:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    session_id = ""
    if created.status_code == 200:
        session_id = str(created.json()["id"])
    return created.status_code, session_id, created.json().get("status", "")


async def test_summary_when_global_and_org_on(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    async def fake_complete(
        settings: Settings,
        *,
        api_key: str | None,
        prompt: str,
        temperature: float = 0,
    ) -> str:
        del settings, api_key, temperature
        prompts.append(prompt)
        return "short summary"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, summary=True),
        store=store,
        harness=ThinkingHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status, session_id, session_status = await _create(client, "org-on")
        assert status == 200
        assert session_status == "idle"
        events = await _wait_idle(client, "org-on", session_id)
    types = [event["type"] for event in events]
    assert "agent.session.turn.thinking.summary.completed" in types
    summary = next(
        event
        for event in events
        if event["type"] == "agent.session.turn.thinking.summary.completed"
    )
    assert summary["data"]["item_id"] == "think-1"
    assert summary["data"]["summary"] == "short summary"
    assert summary["data"]["summary_status"] == "done"
    assert prompts and "hidden thinking text" in prompts[0]
    dumped = str(events)
    assert "hidden thinking text" not in dumped


async def test_no_summary_when_org_off(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "nope"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, summary=True),
        store=store,
        harness=ThinkingHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status, session_id, _session_status = await _create(client, "org-off")
        assert status == 200
        await asyncio.sleep(0.05)
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth("org-off")
        )
    assert called is False
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.thinking.summary.completed" not in types
    assert "agent.session.idle" in types


async def test_no_summary_when_global_off(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "nope"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, summary=False),
        store=store,
        harness=ThinkingHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status, session_id, _session_status = await _create(client, "org-on")
        assert status == 200
        await asyncio.sleep(0.05)
        events = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=_auth("org-on")
        )
    assert called is False
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.thinking.summary.completed" not in types


async def test_summary_failure_does_not_fail_the_turn(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        raise SidekickError("sidekick status")

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, summary=True),
        store=store,
        harness=ThinkingHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        status, session_id, session_status = await _create(client, "org-on")
        assert status == 200
        assert session_status == "idle"
        events = await _wait_idle(client, "org-on", session_id)
    failed = next(
        event
        for event in events
        if event["type"] == "agent.session.turn.thinking.summary.failed"
    )
    assert failed["data"]["summary_status"] == "failed"
    assert "summary" not in failed["data"]
    assert "hidden thinking text" not in str(events)


def test_sidekick_key_prefers_configured() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sidekick_api_key="configured",
    )
    from apipi.services.sidekick import sidekick_key

    assert sidekick_key(settings, "incoming") == "configured"
    bare = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    assert sidekick_key(bare, "incoming") == "incoming"


async def test_sidekick_complete_reads_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    original = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer configured"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "  brief  "}}]}
        )

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            self._client = original(transport=httpx.MockTransport(handler))

        async def __aenter__(self) -> httpx.AsyncClient:
            return self._client

        async def __aexit__(self, *_args: object) -> None:
            await self._client.aclose()

    monkeypatch.setattr("apipi.services.sidekick.httpx.AsyncClient", _Client)
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sidekick_model="m",
        sidekick_base_url="http://sidekick.test/v1",
        sidekick_api_key="configured",
    )
    assert await sidekick_complete(settings, api_key="other", prompt="think") == "brief"
