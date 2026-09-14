from pathlib import Path

from sqlalchemy import text

from apipi.store.engine import create_engine


async def test_file_sqlite_uses_wal(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'apipi.db').resolve().as_posix()}"
    engine = create_engine(url)
    try:
        async with engine.connect() as conn:
            mode = await conn.scalar(text("PRAGMA journal_mode"))
            fk = await conn.scalar(text("PRAGMA foreign_keys"))
        assert str(mode).lower() == "wal"
        assert int(fk) == 1
    finally:
        await engine.dispose()
