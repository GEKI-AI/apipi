from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.support.http import auth
from tests.support.split_worker import api_settings_for

from apipi.common.errors import ApiError
from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store

_PAYLOAD = {
    "object": "list",
    "data": [{"id": "gpt-4.1", "object": "model", "owned_by": "host"}],
}


@pytest.fixture
def model_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url="http://model.test/v1",
    )


@pytest.fixture
async def model_client(
    model_settings: Settings, store: Store
) -> AsyncIterator[AsyncClient]:
    app = create_app(api_settings_for(model_settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_models_requires_bearer(model_client: AsyncClient) -> None:
    response = await model_client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_models_proxies_host(
    model_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(base_url: str, api_key: str | None = None) -> object:
        assert base_url == "http://model.test/v1"
        assert api_key == "t"
        return _PAYLOAD

    monkeypatch.setattr("apipi.services.models.fetch_models_json", fake_fetch)
    response = await model_client.get("/v1/models", headers=auth("t"))
    assert response.status_code == 200
    assert response.json() == _PAYLOAD


async def test_models_uses_overwrite_key(
    tmp_path: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url="http://model.test/v1",
        model_api_key_overwrite="operator-key",
    )

    async def fake_fetch(base_url: str, api_key: str | None = None) -> object:
        del base_url
        assert api_key == "operator-key"
        return _PAYLOAD

    monkeypatch.setattr("apipi.services.models.fetch_models_json", fake_fetch)
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers=auth("tenant-token"))
    assert response.status_code == 200
    assert response.json() == _PAYLOAD


async def test_models_host_unauthorized(
    model_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(base_url: str, api_key: str | None = None) -> object:
        del base_url, api_key
        raise ApiError(
            "invalid_request",
            "Model host rejected the API key",
            code="model_host_unauthorized",
            status_code=401,
        )

    monkeypatch.setattr("apipi.services.models.fetch_models_json", fake_fetch)
    response = await model_client.get("/v1/models", headers=auth("t"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "model_host_unauthorized"


async def test_models_host_unreachable(
    model_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(base_url: str, api_key: str | None = None) -> object:
        del base_url, api_key
        raise ApiError(
            "invalid_request",
            "Model host /models is unreachable",
            code="model_host_unreachable",
            status_code=400,
        )

    monkeypatch.setattr("apipi.services.models.fetch_models_json", boom)
    response = await model_client.get("/v1/models", headers=auth("t"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_host_unreachable"


async def test_models_disabled(tmp_path: Path, store: Store) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url="http://model.test/v1",
        forward_models=False,
    )
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers=auth("t"))
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["type"] == "not_implemented"
    assert body["code"] == "forward_models"
