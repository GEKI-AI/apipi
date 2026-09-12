from httpx import AsyncClient


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_ignores_openai_beta_header(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"OpenAI-Beta": "agents=v1"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_does_not_need_bearer(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
