import stat
from pathlib import Path

import pytest

from apipi.config import Settings
from apipi.dev import (
    _exit_code,
    child_commands,
    child_environments,
    ensure_dev_token,
)
from apipi.services.worker_tokens import authenticate_token, list_tokens, revoke_token
from apipi.store.engine import Store


def _settings(store: Store) -> Settings:
    return Settings(
        database_url=store.engine.url.render_as_string(hide_password=False),
    )


async def test_dev_token_is_created_with_private_mode(
    store: Store, tmp_path: Path
) -> None:
    path = tmp_path / ".apipi" / "dev-worker-token"
    result = await ensure_dev_token(_settings(store), path)
    assert result == path
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert await authenticate_token(store, path.read_text().strip()) is not None


async def test_dev_token_is_reused_while_valid(store: Store, tmp_path: Path) -> None:
    path = tmp_path / ".apipi" / "dev-worker-token"
    await ensure_dev_token(_settings(store), path)
    first = path.read_text()
    await ensure_dev_token(_settings(store), path)
    assert path.read_text() == first
    assert len(await list_tokens(store)) == 1


async def test_dev_token_is_replaced_when_revoked(store: Store, tmp_path: Path) -> None:
    path = tmp_path / ".apipi" / "dev-worker-token"
    await ensure_dev_token(_settings(store), path)
    first = path.read_text()
    (row,) = await list_tokens(store)
    await revoke_token(store, row.id)
    await ensure_dev_token(_settings(store), path)
    assert path.read_text() != first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert await authenticate_token(store, path.read_text().strip()) is not None


async def test_dev_token_is_replaced_when_unknown(store: Store, tmp_path: Path) -> None:
    path = tmp_path / ".apipi" / "dev-worker-token"
    path.parent.mkdir()
    path.write_text("apipi_wk_not-in-this-database")
    await ensure_dev_token(_settings(store), path)
    assert path.read_text() != "apipi_wk_not-in-this-database"
    assert await authenticate_token(store, path.read_text().strip()) is not None


def test_child_environments_split_database_and_share_the_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///dev.db")
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    token = tmp_path / "token"
    api, worker = child_environments(token, host="0.0.0.0", port=9001)
    assert api["DATABASE_URL"] == "sqlite:///dev.db"
    assert "DATABASE_URL" not in worker
    assert api.get("APIPI_LOCAL_STORE_DIR") == worker.get("APIPI_LOCAL_STORE_DIR")
    assert worker["APIPI_API_URL"] == "http://127.0.0.1:9001"
    assert worker["APIPI_WORKER_TOKEN_FILE"] == str(token.resolve())
    assert worker["APIPI_RUN_MODE"] == "none"
    assert "APIPI_WORKER_TOKEN_FILE" not in api


def test_child_environments_keep_user_choices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("APIPI_LOCAL_STORE_DIR", "/srv/store")
    monkeypatch.setenv("APIPI_RUN_MODE", "microvm")
    api, worker = child_environments(tmp_path / "t", host="127.0.0.1", port=8000)
    assert (
        api["APIPI_LOCAL_STORE_DIR"] == worker["APIPI_LOCAL_STORE_DIR"] == "/srv/store"
    )
    assert worker["APIPI_RUN_MODE"] == "microvm"


def test_child_commands_are_two_separate_processes() -> None:
    api, worker = child_commands(config_path=None, host="127.0.0.1", port=8001)
    assert api[-5:] == ["serve", "--host", "127.0.0.1", "--port", "8001"]
    assert worker[-1] == "worker"
    assert "--api-only" not in api


def test_worker_gets_the_config_unless_it_holds_the_database_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    plain = tmp_path / "plain.toml"
    plain.write_text('log_level = "debug"\n')
    api, worker = child_commands(config_path=str(plain), host="h", port=1)
    assert api[-2:] == ["--config", str(plain)]
    assert worker[-2:] == ["--config", str(plain)]
    with_db = tmp_path / "db.toml"
    with_db.write_text('database_url = "sqlite:///dev.db"\n')
    api, worker = child_commands(config_path=str(with_db), host="h", port=1)
    assert api[-2:] == ["--config", str(with_db)]
    assert "--config" not in worker


def test_exit_code_maps_signals_to_the_shell_convention() -> None:
    assert _exit_code(None) == 0
    assert _exit_code(0) == 0
    assert _exit_code(3) == 3
    assert _exit_code(-9) == 137
    assert _exit_code(-15) == 143
