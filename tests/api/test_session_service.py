import asyncio
import uuid

from fastapi import FastAPI
from tests.support.split_worker import api_settings_for, serve_split
from tests.support.waits import until

from apipi.config import Settings
from apipi.env.spec import EnvironmentSpec
from apipi.gateway import Gateway
from apipi.services.agents import AgentWrite
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness


def _app(gateway: Gateway) -> FastAPI:
    app = FastAPI()
    gateway.configure(app)
    app.include_router(gateway.routers.workers)
    return app


async def test_in_process_create_and_stream(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    gateway = Gateway.create(api_settings_for(settings), store=store)
    await gateway.startup()
    try:
        async with serve_split(_app(gateway), settings, FakeHarness(), worker_secret):
            tenant_id = uuid.uuid4()
            await gateway.ensure_tenant(tenant_id)
            created = await gateway.sessions.create(
                tenant_id,
                agent=AgentWrite(name="bot", model="test"),
                environment=EnvironmentSpec(type="none"),
                input="hello",
                key_id="k",
                api_key="t",
            )
            session_id = uuid.UUID(created["id"])
            assert created["status"] == "idle"
            got = await gateway.sessions.get(tenant_id, session_id)
            assert got["id"] == created["id"]
            events: list[dict[str, object]] = []
            async for event in gateway.sessions.stream(tenant_id, session_id):
                events.append(event)
                if event["type"] == "agent.session.idle":
                    break
            types = [event["type"] for event in events]
            assert "agent.session.created" in types
            assert "agent.session.idle" in types
            assert all(event.get("type") != "_keep_alive" for event in events)
    finally:
        await gateway.shutdown()


async def test_create_wait_turn_false_returns_before_turn(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    harness = FakeHarness()
    harness.hold = True
    gateway = Gateway.create(api_settings_for(settings), store=store)
    await gateway.startup()
    try:
        async with serve_split(_app(gateway), settings, harness, worker_secret):
            tenant_id = uuid.uuid4()
            await gateway.ensure_tenant(tenant_id)
            created = await asyncio.wait_for(
                gateway.sessions.create(
                    tenant_id,
                    agent=AgentWrite(name="bot", model="test"),
                    environment=EnvironmentSpec(type="none"),
                    input="hello",
                    wait_turn=False,
                ),
                timeout=2,
            )
            assert created["id"]
            await until(lambda: harness.prompts == ["hello"])
    finally:
        await gateway.shutdown()
