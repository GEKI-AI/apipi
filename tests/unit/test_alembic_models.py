from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

from apipi.store.migrate import upgrade_head
from apipi.store.models import Base


def test_alembic_head_matches_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'apipi.db'}"
    upgrade_head(url)
    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()
    assert diff == []
