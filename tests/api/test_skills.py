from collections.abc import AsyncIterator
from pathlib import Path

from httpx import AsyncClient
from tests.support.http import auth, create_agent

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness


async def _client(
    settings: Settings, store: Store, harness: FakeHarness, worker_secret: str
) -> AsyncIterator[AsyncClient]:
    from tests.support.split_worker import split_client_for

    async with split_client_for(
        settings, store, harness=harness, token=worker_secret
    ) as (_app, client, _worker):
        yield client


def _write_skill(tree: Path, name: str) -> None:
    tree.mkdir(parents=True)
    (tree / "SKILL.md").write_text(f"---\nname: {name}\n---\n")


async def test_planted_skill_is_passed_to_harness(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness, worker_secret):
        token = "skills"
        agent_id = await create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={"agent_id": agent_id, "environment": {"type": "openai_hosted"}},
        )
        assert created.status_code == 200
        from tests.support.workspace import hosted_dir

        directory = hosted_dir(settings, token, created.json()["id"])
        tree = directory / ".agents" / "skills" / "demo"
        _write_skill(tree, "demo")
        posted = await client.post(
            f"/v1/agents/sessions/{created.json()['id']}/events",
            headers=auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert posted.status_code == 200
        assert harness.skill_dirs is not None
        assert str(tree.resolve()) in harness.skill_dirs


async def test_capability_directories_copied_and_discovered(
    settings: Settings, store: Store, tmp_path: Path, worker_secret: str
) -> None:
    harness = FakeHarness()
    caps = tmp_path / "pack"
    _write_skill(caps / "cap-skill", "cap-skill")
    async for client in _client(settings, store, harness, worker_secret):
        token = "caps"
        agent_id = await create_agent(client, token)
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
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
        from tests.support.workspace import hosted_dir

        directory = hosted_dir(settings, token, created.json()["id"])
        copied = directory / "pack" / "cap-skill"
        assert (copied / "SKILL.md").is_file()
        assert harness.skill_dirs is not None
        assert str(copied.resolve()) in harness.skill_dirs


async def test_unknown_environment_field(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    async for client in _client(settings, store, harness, worker_secret):
        token = "skills"
        agent_id = await create_agent(client, token)
        response = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={
                "agent_id": agent_id,
                "environment": {"type": "none", "foo": 1},
            },
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unknown_field"
