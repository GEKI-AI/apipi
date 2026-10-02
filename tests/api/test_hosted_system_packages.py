from httpx import AsyncClient

from apipi.config import Settings


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_system_packages_rejected_on_an_api_with_the_default_run_mode(
    settings: Settings, client: AsyncClient
) -> None:
    assert settings.run_mode == "none"
    token = "system-packages-default-run-mode"
    created = await client.post(
        "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
    )
    response = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created.json()["id"],
            "environment": {
                "type": "openai_hosted",
                "packages": {"system": ["git"]},
            },
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "read-only microvm root" in response.json()["error"]["message"]
