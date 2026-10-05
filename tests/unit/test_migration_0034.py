from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, text

from apipi.store.migrate import alembic_config, upgrade_head


def _indexes(url: str, table: str) -> dict[str, list[str]]:
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            names = [
                row[1]
                for row in connection.execute(text(f"PRAGMA index_list({table})"))
            ]
            return {
                name: [
                    row[2]
                    for row in connection.execute(text(f"PRAGMA index_info({name})"))
                ]
                for name in names
            }
    finally:
        engine.dispose()


def test_creation_order_migration_indexes_turns_items_and_artifacts(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    command.upgrade(alembic_config(url), "0033_env_credentials")
    assert "ix_turns_session_created" not in _indexes(url, "turns")
    assert "ix_items_session_created" not in _indexes(url, "items")
    upgrade_head(url)
    columns = ["tenant_id", "session_id", "created_at"]
    assert _indexes(url, "turns")["ix_turns_session_created"] == columns
    assert _indexes(url, "items")["ix_items_session_created"] == columns
    assert _indexes(url, "artifacts")["ix_artifacts_session_created"] == columns
    command.downgrade(alembic_config(url), "0033_env_credentials")
    assert "ix_turns_session_created" not in _indexes(url, "turns")
    assert "ix_items_session_created" not in _indexes(url, "items")
    assert "ix_artifacts_session_created" not in _indexes(url, "artifacts")
