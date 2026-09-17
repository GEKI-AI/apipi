from pathlib import Path

from alembic import command
from alembic.config import Config

from apipi.config import load_settings, store_url
from apipi.gateway.logutil import configure_logging


def alembic_config(url: str) -> Config:
    root = Path(__file__).resolve().parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(root))
    cfg.set_main_option("sqlalchemy.url", store_url(url).replace("%", "%%"))
    return cfg


def upgrade_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def migrate(*, config_path: str | None = None) -> None:
    settings = load_settings(config_path=config_path)
    configure_logging(level=settings.log_level, format=settings.log_format)
    upgrade_head(settings.database_url)
