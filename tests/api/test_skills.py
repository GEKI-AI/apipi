from collections.abc import AsyncIterator
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _client(
    settings: Settings, store: Store, harness: FakeHarness
) -> AsyncIterator[AsyncClient]:
    app = create_app(settings, store=store, harness=harness)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def _create_agent(client: AsyncClient, token: str) -> str:
    response = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    assert response.status_code == 200
    return str(response.json()["id"])


def _write_skill(tree: Path, name: str) -> None:
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(f"---\nname: {name}\n---\n")


async def test_planted_skill_is_passed_to_harness(
    settings: Settings, store: Store
) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness):
        token = "skills"
        agent_id = await _create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={"agent_id": agent_id, "environment": {"type": "openai_hosted"}},
        )
        assert created.status_code == 200
        directory = Path(created.json()["environment"]["directory"])
        tree = directory / ".agents" / "skills" / "demo"
        _write_skill(tree, "demo")
        posted = await client.post(
            f"/v1/agents/sessions/{created.json()['id']}/events",
            headers=_auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert posted.status_code == 200
        assert harness.skill_dirs is not None
        assert str(tree.resolve()) in harness.skill_dirs


async def test_capability_directories_copied_and_discovered(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    harness = FakeHarness()
    caps = tmp_path / "pack"
    _write_skill(caps / "cap-skill", "cap-skill")
    async for client in _client(settings, store, harness):
        token = "caps"
        agent_id = await _create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {
                    "type": "openai_hosted",
                    "capability_directories": [str(caps)],
                },
                "input": "hello",
            },
        )
        assert created.status_code == 200
        env = created.json()["environment"]
        assert env["capability_directories"] == [str(caps)]
        directory = Path(env["directory"])
        copied = directory / "pack" / "cap-skill"
        assert (copied / "SKILL.md").is_file()
        assert harness.skill_dirs is not None
        assert str(copied.resolve()) in harness.skill_dirs


async def test_unknown_environment_field(settings: Settings, store: Store) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness):
        token = "skills"
        agent_id = await _create_agent(client, token)
        response = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "none", "foo": 1},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_field"
