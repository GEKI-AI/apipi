from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support import auth_plugin

from apipi.config import ConfigError, Settings
from apipi.gateway import create_app
from apipi.gateway.tokens import hash_token
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _settings(tmp_path: Path, plugin: str, ttl: timedelta) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        auth=plugin,
        auth_cache_ttl=ttl,
    )


@pytest.fixture
async def plugin_client(
    store: Store, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[tuple[AsyncClient, FastAPI]]:
    auth_plugin.reset()
    plugin, ttl = request.param
    settings = _settings(tmp_path, plugin, ttl)
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:accept", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_second_request_uses_cache(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, app = plugin_client
    token = "cached-key"
    first = await client.get("/v1/agents", headers=_auth(token))
    second = await client.get("/v1/agents", headers=_auth(token))
    assert first.status_code == 200
    assert second.status_code == 200
    assert auth_plugin.calls == [token]
    keys = list(app.state.auth_cache._entries)
    assert keys == [hash_token(token)]
    assert token not in keys


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:reject", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_reject_is_401(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, _app = plugin_client
    response = await client.get("/v1/agents", headers=_auth("nope"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert auth_plugin.calls == ["nope"]


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:reject_typed", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_typed_401_is_cached(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, app = plugin_client
    first = await client.get("/v1/agents", headers=_auth("expired"))
    second = await client.get("/v1/agents", headers=_auth("expired"))
    assert first.status_code == 401
    assert first.json()["error"] == {
        "type": "invalid_request",
        "code": "unauthorized",
        "message": "Expired key",
    }
    assert second.status_code == 401
    assert auth_plugin.calls == ["expired"]
    cached = list(app.state.auth_cache._entries.values())
    assert len(cached) == 1


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:limit", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_429_is_not_cached(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, app = plugin_client
    first = await client.get("/v1/agents", headers=_auth("hot"))
    second = await client.get("/v1/agents", headers=_auth("hot"))
    assert first.status_code == 429
    assert first.json()["error"] == {
        "type": "invalid_request",
        "code": "rate_limited",
        "message": "Too many requests",
    }
    assert second.status_code == 429
    assert auth_plugin.calls == ["hot", "hot"]
    assert app.state.auth_cache._entries == {}


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:quota", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_quota_dict_is_429(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, _app = plugin_client
    response = await client.get("/v1/agents", headers=_auth("full"))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota"
    assert response.json()["error"]["message"] == "No more agents for this tenant"


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:boom", timedelta(seconds=30))],
    indirect=True,
)
async def test_plugin_error_is_not_cached_as_success(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, app = plugin_client
    first = await client.get("/v1/agents", headers=_auth("x"))
    second = await client.get("/v1/agents", headers=_auth("x"))
    assert first.status_code == 401
    assert second.status_code == 401
    assert auth_plugin.calls == ["x", "x"]
    assert app.state.auth_cache._entries == {}


@pytest.mark.parametrize(
    "plugin_client",
    [("tests.support.auth_plugin:accept", timedelta(seconds=0))],
    indirect=True,
)
async def test_expired_cache_calls_plugin_again(
    plugin_client: tuple[AsyncClient, FastAPI],
) -> None:
    client, _app = plugin_client
    token = "ttl-key"
    await client.get("/v1/agents", headers=_auth(token))
    await client.get("/v1/agents", headers=_auth(token))
    assert auth_plugin.calls == [token, token]


def test_bad_auth_path_is_config_error(store: Store, tmp_path: Path) -> None:
    settings = _settings(tmp_path, "nope.missing:func", timedelta(seconds=30))
    with pytest.raises(ConfigError, match="APIPI_AUTH"):
        create_app(settings, store=store, harness=FakeHarness())
