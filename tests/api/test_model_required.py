import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
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


async def test_unknown_model_rejected_on_agent_write(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_settings = settings.model_copy(
        update={"model_base_url": "http://model.test/v1"}
    )
    app = create_app(api_settings_for(host_settings), store=store)

    async def fake_ids(*_args: object, **_kwargs: object) -> list[str]:
        return ["other"]

    monkeypatch.setattr("apipi.worker.pi.model_host.listed_models", fake_ids)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "unknown-model"
        created = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "missing"},
        )
        assert created.status_code == 400
        assert created.json()["error"]["code"] == "model_not_found"
        saved = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "other"},
        )
        assert saved.status_code == 200
        changed = await client.post(
            f"/v1/agents/{saved.json()['id']}",
            headers=_auth(token),
            json={"model": "missing"},
        )
        assert changed.status_code == 400
        assert changed.json()["error"]["code"] == "model_not_found"
