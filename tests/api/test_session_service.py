import uuid

from apipi.api.agents import AgentWrite
from apipi.config import Settings
from apipi.env.spec import EnvironmentSpec
from apipi.gateway import Gateway
from apipi.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.store.repo import ensure_tenant


async def test_in_process_create_and_stream(settings: Settings, store: Store) -> None:
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    await gateway.startup()
    try:
        tenant_id = uuid.uuid4()
        async with store.session() as db:
            await ensure_tenant(db, tenant_id)
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
