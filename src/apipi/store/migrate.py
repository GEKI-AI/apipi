import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

from apipi.config import Settings, load_settings, store_url
from apipi.gateway.logutil import configure_logging
from apipi.services.vaults import encrypt_plaintext_vault_tokens
from apipi.store.engine import Store, create_engine


def alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    cfg.set_main_option("sqlalchemy.url", store_url(url).replace("%", "%%"))
    return cfg


def upgrade_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


async def _encrypt_vault_tokens(settings: Settings) -> None:
    store = Store(create_engine(settings.database_url, pool_size=settings.db_pool_size))
    try:
        await encrypt_plaintext_vault_tokens(store, settings)
    finally:
        await store.dispose()


def migrate(*, config_path: str | None = None) -> None:
    settings = load_settings(config_path=config_path)
    configure_logging(level=settings.log_level, format=settings.log_format)
    upgrade_head(settings.database_url)
    asyncio.run(_encrypt_vault_tokens(settings))
