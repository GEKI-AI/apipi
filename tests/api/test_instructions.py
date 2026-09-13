from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_saved_agent_instructions_reach_harness(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "saved-instructions"
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test", "instructions": "be brief"},
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
        assert created.status_code == 200
        assert harness.instructions == "be brief"


async def test_inline_instructions_kept_for_follow_up(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "inline-instructions"
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent": {
                    "name": "bot",
                    "model": "test",
                    "instructions": "write tests",
                },
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert harness.instructions == "write tests"
        session_id = created.json()["id"]
        harness.instructions = None
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "again"},
        )
        assert posted.status_code == 200
        assert harness.instructions == "write tests"


async def test_empty_instructions_are_not_forwarded(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "empty-instructions"
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent": {"name": "bot", "model": "test", "instructions": ""},
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert harness.instructions is None


async def test_omitted_instructions_are_not_forwarded(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "omit-instructions"
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent": {"name": "bot", "model": "test"},
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert harness.instructions is None
