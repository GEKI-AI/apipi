import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import tenant_from_key
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.services.sidekick import SidekickError
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _authenticate(token: str) -> dict[str, object]:
    return {
        "key_id": hash_token(token),
        "tenant_id": tenant_from_key(token),
        "auto_title": token == "org-on",
    }


def _settings(tmp_path, *, title: bool) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        auto_title=title,
        sidekick_model="sidekick" if title else None,
        sidekick_api_key="sidekick-key",
    )


async def _events(client: AsyncClient, token: str, session_id: str) -> list[dict]:
    response = await client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert isinstance(data, list)
    return data


async def _wait_title(client: AsyncClient, token: str, session_id: str) -> list[dict]:
    events: list[dict] = []
    for _ in range(50):
        events = await _events(client, token, session_id)
        types = [event["type"] for event in events]
        if "agent.session.title.updated" in types:
            return events
        await asyncio.sleep(0.02)
    return events


async def _create(client: AsyncClient, token: str, metadata: dict | None = None) -> str:
    agent = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert agent.status_code == 200
    body: dict[str, object] = {
        "agent_id": agent.json()["id"],
        "environment": {"type": "none"},
        "input": "plan the release",
    }
    if metadata is not None:
        body["metadata"] = metadata
    created = await client.post("/v1/agents/sessions", headers=_auth(token), json=body)
    assert created.status_code == 200
    assert created.json()["status"] == "idle"
    return str(created.json()["id"])


async def test_title_when_global_and_org_on(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompts: list[str] = []

    async def fake_complete(*_args: object, **kwargs: object) -> str:
        prompt = kwargs.get("prompt")
        assert isinstance(prompt, str)
        prompts.append(prompt)
        return "Release plan"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, title=True),
        store=store,
        harness=FakeHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create(client, "org-on")
        events = await _wait_title(client, "org-on", session_id)
        updated = await client.post(
            f"/v1/agents/sessions/{session_id}",
            headers=_auth("org-on"),
            json={"metadata": {"note": "kept"}},
        )
        assert updated.status_code == 200
        metadata = updated.json()["metadata"]
    assert prompts and "plan the release" in prompts[0]
    title_event = next(
        event for event in events if event["type"] == "agent.session.title.updated"
    )
    data = title_event["data"]
    assert isinstance(data, dict)
    assert data.get("title") == "Release plan"
    assert data.get("title_status") == "done"
    assert metadata["apipi.title"] == "Release plan"
    assert metadata["apipi.title_status"] == "done"
    assert metadata["note"] == "kept"


async def test_no_title_when_org_off(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "Nope"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, title=True),
        store=store,
        harness=FakeHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create(client, "org-off")
        await asyncio.sleep(0.05)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth("org-off")
        )
    assert called is False
    assert "apipi.title" not in session.json()["metadata"]


async def test_no_title_when_global_off(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "Nope"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, title=False),
        store=store,
        harness=FakeHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create(client, "org-on")
        await asyncio.sleep(0.05)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth("org-on")
        )
    assert called is False
    assert "apipi.title" not in session.json()["metadata"]


async def test_existing_title_is_not_overwritten(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        nonlocal called
        called = True
        return "New"

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, title=True),
        store=store,
        harness=FakeHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create(
            client, "org-on", metadata={"apipi.title": "Keep me"}
        )
        await asyncio.sleep(0.05)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth("org-on")
        )
    assert called is False
    assert session.json()["metadata"]["apipi.title"] == "Keep me"


async def test_title_failure_does_not_fail_the_turn(
    tmp_path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_complete(*_args: object, **_kwargs: object) -> str:
        raise SidekickError("sidekick status")

    monkeypatch.setattr("apipi.services.sidekick.sidekick_complete", fake_complete)
    app = create_app(
        _settings(tmp_path, title=True),
        store=store,
        harness=FakeHarness(),
        authenticate=_authenticate,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create(client, "org-on")
        events = await _wait_title(client, "org-on", session_id)
        session = await client.get(
            f"/v1/agents/sessions/{session_id}", headers=_auth("org-on")
        )
    assert session.json()["status"] == "idle"
    assert "apipi.title" not in session.json()["metadata"]
    assert session.json()["metadata"]["apipi.title_status"] == "failed"
    failed = next(
        event for event in events if event["type"] == "agent.session.title.updated"
    )
    data = failed["data"]
    assert isinstance(data, dict)
    assert data.get("title_status") == "failed"
    assert "title" not in data
