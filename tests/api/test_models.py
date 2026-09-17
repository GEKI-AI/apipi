from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store

_PAYLOAD = {
    "object": "list",
    "data": [{"id": "gpt-4.1", "object": "model", "owned_by": "host"}],
}


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


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
    app = create_app(model_settings, store=store, harness=FakeHarness())
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
    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url == "http://model.test/v1/models"
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        assert headers["Authorization"] == "Bearer t"
        return httpx.Response(200, json=_PAYLOAD)

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", fake_get)
    response = await model_client.get("/v1/models", headers=_auth("t"))
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

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        assert headers["Authorization"] == "Bearer operator-key"
        return httpx.Response(200, json=_PAYLOAD)

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", fake_get)
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers=_auth("tenant-token"))
    assert response.status_code == 200
    assert response.json() == _PAYLOAD


async def test_models_host_unauthorized(
    model_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apipi.worker.pi.model_host.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(401, json={"error": "no"}),
    )
    response = await model_client.get("/v1/models", headers=_auth("t"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "model_host_unauthorized"


async def test_models_host_unreachable(
    model_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", boom)
    response = await model_client.get("/v1/models", headers=_auth("t"))
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
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/models", headers=_auth("t"))
    assert response.status_code == 400
    body = response.json()["error"]
    assert body["type"] == "not_implemented"
    assert body["code"] == "forward_models"
