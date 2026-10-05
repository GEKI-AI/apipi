from pathlib import Path

import pytest

from apipi.config import Settings
from tests.support.config import DATABASE_URL


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        local_store_dir=str(tmp_path / "store"),
        search_provider="tavily",
        search_api_key="secret-key",
    )
