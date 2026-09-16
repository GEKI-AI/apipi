import asyncio
import uuid

from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker

from apipi.app import create_app
from apipi.config import Settings
from apipi.execution import RemoteExecution, local_execution
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _api_settings(settings: Settings) -> Settings:
    return Settings(
        database_url=settings.database_url,
        run_mode="none",
        sessions_dir=settings.sessions_dir,
        worker_token="worker-secret",
        api_only=True,
    )


async def test_api_only_uses_remote_execution(settings: Settings, store: Store) -> None:
    app = create_app(_api_settings(settings), store=store, harness=FakeHarness())
    assert isinstance(app.state.execution, RemoteExecution)


async def test_api_only_without_worker_is_429(settings: Settings, store: Store) -> None:
    app = create_app(_api_settings(settings), store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth("t"), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth("t"),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 429
        assert created.json()["error"]["code"] == "capacity"


async def test_remote_turn_via_worker(settings: Settings, store: Store) -> None:
    api_settings = _api_settings(settings)
    app = create_app(api_settings, store=store, harness=FakeHarness())
    local = local_execution(
        api_settings,
        store=store,
        harness=FakeHarness(),
        hub=app.state.event_hub,
        env_hub=app.state.env_hub,
    )
    token = "t"
    worker = FakeWorker(app, "worker-secret")
    ready = asyncio.Event()

    async def pump() -> None:
        hello = await worker.connect(capacity=2)
        assert hello.get("ok") is True
        ready.set()
        while True:
            message = await worker.receive_json()
            if message.get("type") != "command":
                continue
            await worker.send_json(
                {
                    "type": "lease.ack",
                    "id": message.get("id"),
                    "lease_id": message.get("lease_id"),
                }
            )
            from apipi.worker import dispatch_command

            await dispatch_command(local, message)

    task = asyncio.create_task(pump())
    await ready.wait()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            agent = await client.post(
                "/v1/agents",
                headers=_auth(token),
                json={"name": "bot", "model": "test"},
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
            body = created.json()
            assert body["status"] == "idle"
            session_id = uuid.UUID(body["id"])
            events = await client.get(
                f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
            )
            types = [event["type"] for event in events.json()["data"]]
            assert "agent.session.turn.completed" in types
    finally:
        task.cancel()
        await worker.close()
        await local.close()
