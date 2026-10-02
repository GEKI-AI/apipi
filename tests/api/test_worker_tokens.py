import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from tests.support.fake_worker import FakeWorker

from apipi.cli import main, prepare_worker
from apipi.config import ConfigError, Settings, load_settings, load_worker_token
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.services.worker_tokens import (
    authenticate_token,
    create_token,
    hash_worker_secret,
    list_tokens,
    revoke_token,
)
from apipi.store.engine import Store


async def test_create_stores_only_the_hash(store: Store) -> None:
    created = await create_token(store, name="w1")
    assert created.secret
    rows = await list_tokens(store)
    assert len(rows) == 1
    assert rows[0].name == "w1"
    assert rows[0].token_hash == hashlib.sha256(created.secret.encode()).hexdigest()
    assert rows[0].token_hash != created.secret
    assert rows[0].revoked_at is None


async def test_use_updates_last_used_and_binds_worker(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    rows = await list_tokens(store)
    assert len(rows) == 1
    assert rows[0].last_used_at is not None
    assert rows[0].worker_id == worker_id
    await worker.close()


async def test_revoke_closes_socket_and_rejects_register(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, worker_secret)
    hello = await worker.connect()
    assert hello["ok"] is True
    rows = await list_tokens(store)
    revoked = await revoke_token(store, rows[0].id)
    assert revoked is not None
    assert revoked.revoked_at is not None
    await worker.send_json({"type": "heartbeat"})
    closed = await worker.wait_close()
    assert closed["code"] == 1008
    assert closed["reason"] == "revoked"
    await worker.close()
    second = FakeWorker(app, worker_secret)
    await second.connect()
    assert second.hello is not None
    assert second.hello.get("ok") is False
    assert second.hello.get("error") == "revoked"
    await second.close()


async def test_rotate_without_downtime(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    first = FakeWorker(app, worker_secret)
    hello = await first.connect()
    assert hello["ok"] is True
    worker_id = uuid.UUID(str(hello["worker_id"]))
    rotated = await create_token(store, name="rotated", worker_id=worker_id)
    second = FakeWorker(app, rotated.secret, worker_id=str(worker_id))
    hello2 = await second.connect()
    assert hello2["ok"] is True
    assert hello2["worker_id"] == str(worker_id)
    rows = await list_tokens(store)
    old = next(row for row in rows if row.name == "test-worker")
    await revoke_token(store, old.id)
    await second.send_json({"type": "heartbeat"})
    for _ in range(50):
        live = app.state.workers.get(worker_id)
        if live is not None and live.token_id is not None:
            break
        await asyncio.sleep(0.02)
    live = app.state.workers.get(worker_id)
    assert live is not None
    await second.close()
    await first.close()


async def test_token_bound_to_live_worker_rejects_other_id(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    first = FakeWorker(app, worker_secret)
    hello = await first.connect()
    assert hello["ok"] is True
    other = FakeWorker(app, worker_secret, worker_id=str(uuid.uuid4()))
    await other.connect()
    assert other.hello is not None
    assert other.hello.get("ok") is False
    await other.close()
    await first.close()
    first_id = uuid.UUID(str(hello["worker_id"]))
    for _ in range(50):
        if app.state.workers.get(first_id) is None:
            break
        await asyncio.sleep(0.02)
    rebound = FakeWorker(app, worker_secret, worker_id=str(uuid.uuid4()))
    hello2 = await rebound.connect()
    assert hello2["ok"] is True
    rows = await list_tokens(store)
    assert rows[0].worker_id == uuid.UUID(str(hello2["worker_id"]))
    await rebound.close()


async def test_token_declared_at_creation(settings: Settings, store: Store) -> None:
    worker_id = uuid.uuid4()
    created = await create_token(store, name="pinned", worker_id=worker_id)
    app = create_app(settings, store=store, harness=FakeHarness())
    worker = FakeWorker(app, created.secret, worker_id=str(worker_id))
    hello = await worker.connect()
    assert hello["ok"] is True
    stranger = FakeWorker(app, created.secret, worker_id=str(uuid.uuid4()))
    await stranger.connect()
    assert stranger.hello is not None
    assert stranger.hello.get("ok") is False
    await stranger.close()
    await worker.close()
    for _ in range(50):
        if app.state.workers.get(worker_id) is None:
            break
        await asyncio.sleep(0.02)
    late = FakeWorker(app, created.secret, worker_id=str(uuid.uuid4()))
    late_hello = await late.connect()
    assert late_hello["ok"] is True
    await late.close()


async def test_unknown_token_is_unauthorized(settings: Settings, store: Store) -> None:
    assert await authenticate_token(store, "no-such-token") is None
    assert hash_worker_secret("abc") == hashlib.sha256(b"abc").hexdigest()


async def test_worker_token_rejected_on_public_routes(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(settings, store=store, harness=FakeHarness())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post(
            "/v1/agents",
            headers={"Authorization": f"Bearer {worker_secret}"},
            json={"name": "bot", "model": "test"},
        )
        assert denied.status_code == 401
        body = denied.json()["error"]
        assert body["code"] == "unauthorized"
        assert "only on /internal/worker" in body["message"]


def test_prepare_worker_requires_token_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="APIPI_WORKER_TOKEN_FILE is required"):
        prepare_worker(
            Settings(
                database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
                run_mode="none",
            )
        )


def test_prepare_worker_rejects_missing_and_empty_file(tmp_path: Path) -> None:

    missing = Path(tmp_path) / "missing.token"
    with pytest.raises(ConfigError, match="cannot read APIPI_WORKER_TOKEN_FILE"):
        prepare_worker(
            Settings(
                database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
                run_mode="none",
                worker_token_file=str(missing),
            )
        )
    empty = Path(tmp_path) / "empty.token"
    empty.write_text("  \n")
    with pytest.raises(ConfigError, match="APIPI_WORKER_TOKEN_FILE is empty"):
        prepare_worker(
            Settings(
                database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
                run_mode="none",
                worker_token_file=str(empty),
            )
        )


def test_legacy_token_env_fails_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = Path(tmp_path) / "worker.token"
    token_file.write_text("secret\n")
    monkeypatch.setenv("APIPI_WORKER_TOKEN", "stale-secret")
    monkeypatch.setenv("APIPI_WORKER_TOKEN_FILE", str(token_file))
    with pytest.raises(ConfigError, match="apipi workers token create"):
        load_settings()
    with pytest.raises(ValidationError, match="apipi workers token create"):
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            worker_token_file=str(token_file),
        )


def test_load_worker_token_trims_whitespace(tmp_path: Path) -> None:
    token_file = Path(tmp_path) / "worker.token"
    token_file.write_text("  secret\n")
    assert load_worker_token(str(token_file)) == "secret"


def _cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio

    from apipi.store.engine import create_engine
    from apipi.store.models import Base

    url = f"sqlite+aiosqlite:///{tmp_path}/cli.db"

    async def _init() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(_init())
    monkeypatch.setenv("DATABASE_URL", url)


def test_cli_token_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli_db(monkeypatch, tmp_path)
    assert main(["workers", "token", "create", "--name", "w1"]) == 0
    out = capsys.readouterr()
    secret = out.out.strip()
    assert secret
    assert len(secret.splitlines()) == 1
    assert main(["workers", "token", "list"]) == 0
    listed = capsys.readouterr().out
    assert "w1" in listed
    assert secret not in listed
    token_id = listed.splitlines()[1].split("\t")[0]
    assert main(["workers", "token", "revoke", token_id]) == 0
    relisted = capsys.readouterr().out
    assert main(["workers", "token", "list"]) == 0
    relisted = capsys.readouterr().out
    assert token_id in relisted
    assert main(["workers", "token", "revoke", "missing"]) == 1
