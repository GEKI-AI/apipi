import asyncio
import time
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.gateway.auth import AuthFilter, AuthIdentity, AuthReject
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _settings(tmp_path: Path, auth: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        auth=auth,
    )


async def _client(settings: Settings, store: Store) -> AsyncClient:
    app = create_app(settings, store=store, harness=FakeHarness())
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_off_loop_sync_plugin_does_not_serialise(
    store: Store, tmp_path: Path
) -> None:
    def slow_block(bearer: str) -> dict[str, object]:
        time.sleep(0.2)
        import uuid

        return {
            "key_id": bearer,
            "tenant_id": uuid.uuid4(),
            "cache_key": f"{bearer}-{time.monotonic_ns()}",
        }

    import tests.support.auth_plugin as plugin_mod

    plugin_mod.slow_block = slow_block  # ty: ignore[invalid-assignment]
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:slow_block")
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        start = time.monotonic()
        results = await asyncio.gather(
            *[client.get("/v1/agents", headers=_auth(f"k{i}")) for i in range(4)]
        )
        elapsed = time.monotonic() - start
    assert all(r.status_code == 200 for r in results)
    assert elapsed < 0.8


async def test_async_plugin_supported(store: Store, tmp_path: Path) -> None:
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:async_accept")
    async with await _client(settings, store) as client:
        response = await client.get("/v1/agents", headers=_auth("a-key"))
        assert response.status_code == 200


async def test_single_flight_calls_plugin_once(store: Store, tmp_path: Path) -> None:
    import asyncio
    import uuid
    from datetime import timedelta

    from apipi.gateway.auth import (
        AuthCache,
        AuthIdentity,
        _authenticate_single_flight,
    )

    calls: list[str] = []

    def counting(token: str) -> dict[str, object]:
        calls.append(token)
        return {"key_id": token, "tenant_id": uuid.uuid4()}

    cache = AuthCache(timedelta(seconds=30), max_entries=100)
    ctx = await asyncio.to_thread(lambda: None)  # placeholder
    from types import MappingProxyType

    from apipi.gateway.auth import AuthRequest

    ctx = AuthRequest(method="GET", path="/v1/agents", headers=MappingProxyType({}))
    results = await asyncio.gather(
        *[
            _authenticate_single_flight(counting, "same-key", ctx, cache)
            for _ in range(6)
        ]
    )
    assert all(isinstance(r, AuthIdentity) for r in results)
    assert calls == ["same-key"]


async def test_cache_hit_makes_no_db_call(store: Store, tmp_path: Path) -> None:
    import tests.support.auth_plugin as plugin_mod

    plugin_mod.reset()
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:accept")
    app = create_app(settings, store=store, harness=FakeHarness())
    gateway = app.state.gateway
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/v1/agents", headers=_auth("hit-key"))
        assert first.status_code == 200
        assert len(gateway._tenant_cache) == 1
        cached_tenant = next(iter(gateway._tenant_cache.values()))
        second = await client.get("/v1/agents", headers=_auth("hit-key"))
        assert second.status_code == 200
    assert plugin_mod.calls == ["hit-key"]
    assert len(gateway._tenant_cache) == 1
    assert next(iter(gateway._tenant_cache.values())) is cached_tenant


async def test_gateway_invalidation_reauthenticates(
    store: Store, tmp_path: Path
) -> None:
    import tests.support.auth_plugin as plugin_mod

    plugin_mod.reset()
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:accept")
    app = create_app(settings, store=store, harness=FakeHarness())
    gateway = app.state.gateway
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/v1/agents", headers=_auth("rev-key"))
        assert plugin_mod.calls == ["rev-key"]
        assert gateway.invalidate_auth("rev-key") is True
        await client.get("/v1/agents", headers=_auth("rev-key"))
        assert plugin_mod.calls == ["rev-key", "rev-key"]
        assert gateway.clear_auth_cache() >= 1


async def test_http_invalidate_is_tenant_scoped(store: Store, tmp_path: Path) -> None:
    import uuid

    import tests.support.auth_plugin as plugin_mod

    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    def scoped(bearer: str) -> dict[str, object]:
        tid = tenant_a if bearer.startswith("a-") else tenant_b
        return {"key_id": bearer, "tenant_id": tid}

    plugin_mod.scoped = scoped  # ty: ignore[invalid-assignment]
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:scoped")
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get("/v1/agents", headers=_auth("a-one"))
        ).status_code == 200
        assert (
            await client.get("/v1/agents", headers=_auth("b-one"))
        ).status_code == 200
        response = await client.post(
            "/v1/apipi/auth/invalidate", headers=_auth("a-one"), json={}
        )
        assert response.status_code == 200
        assert response.json() == {"invalidated": 1}
        # b-one entry survives: plugin would fail if called again
        calls_before = len(plugin_mod.calls)
        assert (
            await client.get("/v1/agents", headers=_auth("b-one"))
        ).status_code == 200
        assert len(plugin_mod.calls) == calls_before


async def test_authorize_restricts_to_one_agent(store: Store, tmp_path: Path) -> None:
    import uuid

    import tests.support.auth_plugin as plugin_mod

    tenant = uuid.uuid4()
    allowed: dict[str, str] = {}

    def scoped(bearer: str) -> dict[str, object]:
        return {"key_id": bearer, "tenant_id": tenant}

    async def authorize(
        identity: AuthIdentity, action: str, resource_type: str, resource_id: str | None
    ) -> AuthFilter | AuthReject | None:
        only = allowed.get(identity.key_id)
        if action in ("agent.list", "session.list"):
            if only is None:
                return None
            return AuthFilter(ids=frozenset({only}))
        if resource_type == "agent" and resource_id is not None:
            if only is not None and resource_id != only:
                return AuthReject(
                    status_code=403, code="forbidden", message="Forbidden"
                )
            return None
        if resource_type == "agent" and resource_id is None and action == "agent.run":
            return None
        return None

    plugin_mod.scoped = scoped  # ty: ignore[invalid-assignment]
    settings = _settings(tmp_path, auth="tests.support.auth_plugin:scoped")
    app = create_app(settings, store=store, harness=FakeHarness(), authorize=authorize)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = _auth("worker")
        first = await client.post("/v1/agents", headers=headers, json={"name": "a1"})
        second = await client.post("/v1/agents", headers=headers, json={"name": "a2"})
        assert first.status_code == 200
        assert second.status_code == 200
        id1 = first.json()["id"]
        id2 = second.json()["id"]
        allowed["worker"] = id1
        listed = await client.get("/v1/agents", headers=headers)
        assert listed.status_code == 200
        assert [a["id"] for a in listed.json()["data"]] == [id1]
        # session create with wrong agent -> 403
        bad = await client.post(
            "/v1/agents/sessions", headers=headers, json={"agent_id": id2}
        )
        assert bad.status_code == 403
        good = await client.post(
            "/v1/agents/sessions", headers=headers, json={"agent_id": id1}
        )
        assert good.status_code == 200
        sid = good.json()["id"]
        # other agent session: create via a second key outside the filter
        other = await client.post(
            "/v1/agents/sessions", headers=_auth("admin"), json={"agent_id": id2}
        )
        assert other.status_code == 200
        other_id = other.json()["id"]
        reading = await client.get(f"/v1/agents/sessions/{other_id}", headers=headers)
        assert reading.status_code == 403
        sessions = await client.get("/v1/agents/sessions", headers=headers)
        assert sessions.status_code == 200
        assert [s["id"] for s in sessions.json()["data"]] == [sid]
        # continue with wrong session -> 403
        cont = await client.post(
            f"/v1/agents/sessions/{other_id}/events",
            headers=headers,
            json={"type": "agent.session.input.message", "content": "hi"},
        )
        assert cont.status_code == 403
