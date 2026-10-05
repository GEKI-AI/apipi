import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tests.support.migrations import SqliteRevisions

from apipi.store.migrate import alembic_config, upgrade_head

_NOW = "2026-01-01T00:00:00+00:00"


def _seed(url: str) -> dict[str, str]:
    ids = {
        "tenant": uuid.uuid4().hex,
        "vault": uuid.uuid4().hex,
        "credential": uuid.uuid4().hex,
    }
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name, created_at) VALUES (:id, 't', :now)"),
            {"id": ids["tenant"], "now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO vaults (id, tenant_id, name, metadata, created_at,"
                " updated_at) VALUES (:id, :tenant, 'v', '{}', :now, :now)"
            ),
            {"id": ids["vault"], "tenant": ids["tenant"], "now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO vault_credentials (id, tenant_id, vault_id, name,"
                " auth_type, mcp_server_url, token, created_at, updated_at)"
                " VALUES (:id, :tenant, :vault, 'c', 'static_bearer',"
                " 'https://mcp.example.com/mcp', 'v1:abc', :now, :now)"
            ),
            {
                "id": ids["credential"],
                "tenant": ids["tenant"],
                "vault": ids["vault"],
                "now": _NOW,
            },
        )
    engine.dispose()
    return ids


def _insert_env(connection, ids: dict[str, str], secret_name: str) -> None:
    connection.execute(
        text(
            "INSERT INTO vault_credentials (id, tenant_id, vault_id, auth_type,"
            " token, secret_name, allowed_hosts, metadata, created_at, updated_at)"
            " VALUES (:id, :tenant, :vault, 'environment_variable', 'v1:x',"
            " :name, '[\"github.com\"]', '{}', :now, :now)"
        ),
        {
            "id": uuid.uuid4().hex,
            "tenant": ids["tenant"],
            "vault": ids["vault"],
            "name": secret_name,
            "now": _NOW,
        },
    )


def test_env_credentials_migration_keeps_rows_and_adds_columns(
    tmp_path: Path,
    sqlite_revisions: SqliteRevisions,
) -> None:
    url = sqlite_revisions.copy_at("0032_session_file_paths", tmp_path / "apipi.db")
    ids = _seed(url)
    upgrade_head(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(vault_credentials)")
                )
            }
            assert {"secret_name", "allowed_hosts", "metadata"} <= columns
            row = connection.execute(
                text(
                    "SELECT auth_type, mcp_server_url, token, secret_name,"
                    " allowed_hosts, metadata FROM vault_credentials WHERE id = :id"
                ),
                {"id": ids["credential"]},
            ).one()
            assert tuple(row) == (
                "static_bearer",
                "https://mcp.example.com/mcp",
                "v1:abc",
                None,
                None,
                "{}",
            )
            _insert_env(connection, ids, "GITHUB_TOKEN")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_env(connection, ids, "GITHUB_TOKEN")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO vault_credentials (id, tenant_id, vault_id,"
                    " auth_type, token, metadata, created_at, updated_at)"
                    " VALUES (:id, :tenant, :vault, 'basic', 'x', '{}', :now, :now)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "tenant": ids["tenant"],
                    "vault": ids["vault"],
                    "now": _NOW,
                },
            )
    finally:
        engine.dispose()


def test_env_credentials_migration_downgrade_drops_env_rows(
    tmp_path: Path, sqlite_revisions: SqliteRevisions
) -> None:
    url = sqlite_revisions.copy_at("0032_session_file_paths", tmp_path / "apipi.db")
    ids = _seed(url)
    upgrade_head(url)
    engine = create_engine(url)
    with engine.begin() as connection:
        _insert_env(connection, ids, "GITHUB_TOKEN")
    engine.dispose()
    command.downgrade(alembic_config(url), "0032_session_file_paths")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            kinds = [
                row[0]
                for row in connection.execute(
                    text("SELECT auth_type FROM vault_credentials")
                )
            ]
            assert kinds == ["static_bearer"]
            columns = {
                row[1]
                for row in connection.execute(
                    text("PRAGMA table_info(vault_credentials)")
                )
            }
            assert "secret_name" not in columns
    finally:
        engine.dispose()
