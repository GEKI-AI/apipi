import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tests.support.migrations import SqliteRevisions

from apipi.store.migrate import upgrade_head


def test_worker_ingest_migration_adds_cursor_and_ledger(
    tmp_path: Path, sqlite_revisions: SqliteRevisions
) -> None:
    url = sqlite_revisions.copy_at("0025_worker_tokens", tmp_path / "apipi.db")
    tenant = uuid.uuid4().hex
    session = uuid.uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, created_at)"
                " VALUES (:id, 't', '2026-01-01T00:00:00+00:00')"
            ),
            {"id": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, status, environment,"
                " metadata, required_actions, key_id, vault_ids,"
                " created_at, updated_at)"
                " VALUES (:id, :tenant, 'idle', '{}', '{}', '[]', '', '[]',"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            ),
            {"id": session, "tenant": tenant},
        )
    engine.dispose()
    upgrade_head(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(sessions)"))
            }
            assert "worker_seq" in columns
            tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
            assert "worker_ingest" in tables
            row = connection.execute(
                text("SELECT worker_seq FROM sessions WHERE id = :id"), {"id": session}
            ).one()
            assert row[0] == 0
            connection.execute(
                text(
                    "INSERT INTO worker_ingest (session_id, worker_seq, envelope_type)"
                    " VALUES (:session, 1, 'event')"
                ),
                {"session": session},
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO worker_ingest (session_id, worker_seq,"
                        " envelope_type) VALUES (:session, 1, 'event')"
                    ),
                    {"session": session},
                )
    finally:
        engine.dispose()
