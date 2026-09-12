from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from apipi.app import create_app
from apipi.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
    )


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_ignores_openai_beta_header(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"OpenAI-Beta": "agents=v1"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
