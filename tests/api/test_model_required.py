import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings
from apipi.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_live_turn_requires_model(client: AsyncClient) -> None:
    token = "no-model"
    agent = await client.post("/v1/agents", headers=_auth(token), json={"name": "bot"})
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 400
    body = created.json()
    assert body["error"]["code"] == "model_required"


async def test_unknown_model_on_host(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_settings = settings.model_copy(
        update={"model_base_url": "http://model.test/v1"}
    )
    app = create_app(host_settings, store=store, harness=FakeHarness())
    monkeypatch.setattr(
        "apipi.runtime.listed_models", lambda *_args, **_kwargs: ["other"]
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "unknown-model"
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent": {"name": "bot", "model": "missing"},
                "environment": {"type": "none"},
                "input": "hello",
            },
        )
        assert created.status_code == 400
        assert created.json()["error"]["code"] == "model_not_found"
