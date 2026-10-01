import uuid
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, text

from apipi.store.migrate import alembic_config, upgrade_head


def test_drop_agent_versions_removes_snapshots_keeps_agents(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    command.upgrade(alembic_config(url), "0021_agent_revision")
    tenant = uuid.uuid4().hex
    agent = uuid.uuid4().hex
    version = uuid.uuid4().hex
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
                "INSERT INTO agents (id, tenant_id, name, metadata, tools,"
                " version_seq, revision, created_at, updated_at)"
                " VALUES (:id, :tenant, 'bot', '{}', '[]', 2, 3,"
                " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
            ),
            {"id": agent, "tenant": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO agent_versions (id, tenant_id, agent_id, number,"
                " definition, source, created_at)"
                " VALUES (:id, :tenant, :agent, 1, '{}', 'manual',"
                " '2026-01-01T00:00:00+00:00')"
            ),
            {"id": version, "tenant": tenant, "agent": agent},
        )
    engine.dispose()
    upgrade_head(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
            assert "agent_versions" not in tables
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(agents)"))
            }
            assert "version_seq" not in columns
            assert "revision" not in columns
            rows = connection.execute(text("SELECT id, name FROM agents")).all()
            assert [(item[0], item[1]) for item in rows] == [(agent, "bot")]
    finally:
        engine.dispose()
