from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.pi.platform_prompt import compose_instructions
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
        assert harness.instructions == compose_instructions(settings, "be brief")


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
        expected = compose_instructions(settings, "write tests")
        assert harness.instructions == expected
        session_id = created.json()["id"]
        harness.instructions = None
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "again"},
        )
        assert posted.status_code == 200
        assert harness.instructions == expected


async def test_empty_agent_instructions_keep_platform_prompt(
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
        assert harness.instructions == compose_instructions(settings, "")


async def test_omitted_agent_instructions_keep_platform_prompt(
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
        assert harness.instructions == compose_instructions(settings, None)


async def test_empty_main_platform_prompt_keeps_additional(
    store: Store, tmp_path: Path
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        platform_prompt="",
        platform_prompt_additional="Always answer in German.",
    )
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("empty-main"),
            json={
                "agent": {
                    "name": "bot",
                    "model": "test",
                    "instructions": "be brief",
                },
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert harness.instructions == compose_instructions(settings, "be brief")


async def test_override_main_platform_prompt(store: Store, tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        platform_prompt="Use outputs/ only.",
    )
    harness = FakeHarness()
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("override-main"),
            json={
                "agent": {"name": "bot", "model": "test"},
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
        assert harness.instructions == "Use outputs/ only."
