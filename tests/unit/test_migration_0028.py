import uuid
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from apipi.store.migrate import alembic_config, upgrade_head

_NOW = "2026-01-01T00:00:00+00:00"


def _seed(url: str) -> dict[str, str]:
    ids = {
        "tenant": uuid.uuid4().hex,
        "session": uuid.uuid4().hex,
        "turn": uuid.uuid4().hex,
        "log": uuid.uuid4().hex,
        "rollup": uuid.uuid4().hex,
        "item": uuid.uuid4().hex,
    }
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO tenants (id, name, created_at) VALUES (:id, 't', :now)"),
            {"id": ids["tenant"], "now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, tenant_id, status, environment,"
                " metadata, required_actions, key_id, vault_ids,"
                " created_at, updated_at)"
                " VALUES (:id, :tenant, 'idle', '{}', '{}', '[]', '', '[]',"
                " :now, :now)"
            ),
            {"id": ids["session"], "tenant": ids["tenant"], "now": _NOW},
        )
        connection.execute(
            text(
                "INSERT INTO turns (id, tenant_id, session_id, status,"
                " created_at, updated_at)"
                " VALUES (:id, :tenant, :session, 'completed', :now, :now)"
            ),
            {
                "id": ids["turn"],
                "tenant": ids["tenant"],
                "session": ids["session"],
                "now": _NOW,
            },
        )
        connection.execute(
            text(
                "INSERT INTO turn_logs (id, tenant_id, session_id, turn_id, status,"
                " latency_ms, prompt_tokens, completion_tokens, cache_read_tokens,"
                " cache_write_tokens, total_tokens, tool_names, tool_counts,"
                " mcp_names, mcp_counts, key_id, environment_type, run_mode,"
                " artifact_bytes, created_at)"
                " VALUES (:id, :tenant, :session, :turn, 'completed', 1, 0, 0, 0,"
                " 0, 0, '[]', '{}', '[]', '{}', '', '', '', 0, :now)"
            ),
            {
                "id": ids["log"],
                "tenant": ids["tenant"],
                "session": ids["session"],
                "turn": ids["turn"],
                "now": _NOW,
            },
        )
        connection.execute(
            text(
                "INSERT INTO usage_rollups (id, tenant_id, day, prompt_tokens,"
                " completion_tokens, cache_read_tokens, cache_write_tokens,"
                " total_tokens, turns, artifact_bytes)"
                " VALUES (:id, :tenant, '2026-01-01', 0, 0, 0, 0, 0, 1, 0)"
            ),
            {"id": ids["rollup"], "tenant": ids["tenant"]},
        )
        connection.execute(
            text(
                "INSERT INTO items (id, tenant_id, session_id, turn_id, type, data,"
                " created_at) VALUES (:id, :tenant, :session, :turn, 'message',"
                " '{}', :now)"
            ),
            {
                "id": ids["item"],
                "tenant": ids["tenant"],
                "session": ids["session"],
                "turn": ids["turn"],
                "now": _NOW,
            },
        )
    engine.dispose()
    return ids


def _insert_item(connection, ids: dict[str, str], kind: str) -> None:
    connection.execute(
        text(
            "INSERT INTO items (id, tenant_id, session_id, turn_id, type, data,"
            " created_at) VALUES (:id, :tenant, :session, :turn, :kind, '{}', :now)"
        ),
        {
            "id": uuid.uuid4().hex,
            "tenant": ids["tenant"],
            "session": ids["session"],
            "turn": ids["turn"],
            "kind": kind,
            "now": _NOW,
        },
    )


def test_search_usage_migration_adds_columns_table_and_item_type(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    command.upgrade(alembic_config(url), "0027_artifact_uploads")
    ids = _seed(url)
    upgrade_head(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            for table in ("turn_logs", "usage_rollups"):
                columns = {
                    row[1]
                    for row in connection.execute(text(f"PRAGMA table_info({table})"))
                }
                assert {"search_calls", "search_units"} <= columns
            log = connection.execute(
                text(
                    "SELECT search_calls, search_units, search_counts"
                    " FROM turn_logs WHERE id = :id"
                ),
                {"id": ids["log"]},
            ).one()
            assert log[0] == 0
            assert log[1] == 0
            assert log[2] == "{}"
            rollup = connection.execute(
                text("SELECT search_calls, search_units FROM usage_rollups"),
            ).one()
            assert tuple(rollup) == (0, 0)
            insert = text(
                "INSERT INTO search_turn_counts (id, tenant_id, session_id,"
                " turn_id, provider, key_source, calls, units)"
                " VALUES (:id, :tenant, :session, :turn, 'tavily', 'operator',"
                " 1, 1)"
            )
            params = {
                "tenant": ids["tenant"],
                "session": ids["session"],
                "turn": ids["turn"],
            }
            connection.execute(insert, {"id": uuid.uuid4().hex, **params})
            with pytest.raises(IntegrityError):
                connection.execute(insert, {"id": uuid.uuid4().hex, **params})
    finally:
        engine.dispose()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            _insert_item(connection, ids, "web_search_call")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_item(connection, ids, "bogus")
    finally:
        engine.dispose()


def test_search_usage_migration_cascades_with_turn(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    command.upgrade(alembic_config(url), "0027_artifact_uploads")
    ids = _seed(url)
    upgrade_head(url)
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            connection.execute(
                text(
                    "INSERT INTO search_turn_counts (id, tenant_id, session_id,"
                    " turn_id, provider, key_source, calls, units)"
                    " VALUES (:id, :tenant, :session, :turn, 'staan', 'operator',"
                    " 1, 1)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "tenant": ids["tenant"],
                    "session": ids["session"],
                    "turn": ids["turn"],
                },
            )
            connection.execute(
                text("DELETE FROM turn_logs WHERE turn_id = :turn"),
                {"turn": ids["turn"]},
            )
            connection.execute(
                text("DELETE FROM items WHERE turn_id = :turn"), {"turn": ids["turn"]}
            )
            connection.execute(
                text("DELETE FROM turns WHERE id = :turn"), {"turn": ids["turn"]}
            )
            left = connection.execute(
                text("SELECT count(*) FROM search_turn_counts")
            ).scalar_one()
            assert left == 0
    finally:
        engine.dispose()


def test_search_usage_migration_downgrade_drops_web_search_items(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    command.upgrade(alembic_config(url), "0027_artifact_uploads")
    ids = _seed(url)
    upgrade_head(url)
    engine = create_engine(url)
    with engine.begin() as connection:
        _insert_item(connection, ids, "web_search_call")
    engine.dispose()
    command.downgrade(alembic_config(url), "0027_artifact_uploads")
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            kinds = {
                row[0] for row in connection.execute(text("SELECT type FROM items"))
            }
            assert kinds == {"message"}
            tables = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
            assert "search_turn_counts" not in tables
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(turn_logs)"))
            }
            assert "search_calls" not in columns
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_item(connection, ids, "web_search_call")
    finally:
        engine.dispose()
