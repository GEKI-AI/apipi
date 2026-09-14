import asyncio
import sys
from dataclasses import dataclass
from typing import TextIO

import asyncpg
from sqlalchemy import text

from apipi import __version__
from apipi.auth import load_authenticate
from apipi.config import (
    ConfigError,
    Settings,
    is_sqlite_url,
    load_settings,
    postgres_url,
    require_run_mode,
)
from apipi.pi.isolation import load_isolation
from apipi.pi.model_host import fetch_model_ids, installed_pi_version, require_pinned_pi
from apipi.pi.probe import probe_run_mode
from apipi.pi.version import PINNED_PI
from apipi.store.engine import create_engine


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str = ""


def _line(check: Check) -> str:
    extra = f"  {check.detail}" if check.detail else ""
    return f"{check.status:<4} {check.name}{extra}"


def _database_detail(url: str) -> str:
    if is_sqlite_url(url):
        path = url.split("sqlite+aiosqlite:///", 1)[-1]
        return f"sqlite {path}"
    host = url.split("@")[-1] if "@" in url else url
    return f"postgres {host}"


def ping_store(url: str) -> None:
    if is_sqlite_url(url):

        async def _ping_sqlite() -> None:
            engine = create_engine(url)
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            finally:
                await engine.dispose()

        asyncio.run(_ping_sqlite())
        return
    dsn = postgres_url(url).replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _ping_postgres() -> None:
        conn = await asyncpg.connect(dsn=dsn, timeout=5)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    asyncio.run(_ping_postgres())


def run_checks(
    settings: Settings,
    *,
    skip_db: bool = False,
    skip_model: bool = False,
    fast: bool = False,
) -> list[Check]:
    checks: list[Check] = []
    checks.append(Check("ok", "apipi", __version__))
    try:
        require_pinned_pi(settings)
        version = installed_pi_version(settings) or PINNED_PI
        checks.append(Check("ok", "pi", version))
    except ConfigError as exc:
        checks.append(Check("fail", "pi", str(exc)))
    url = settings.database_url
    if skip_db:
        checks.append(Check("skip", "database", "--skip-db"))
    else:
        try:
            ping_store(url)
            checks.append(Check("ok", "database", _database_detail(url)))
        except ConfigError as exc:
            checks.append(Check("fail", "database", str(exc)))
        except (OSError, asyncpg.PostgresError, TimeoutError):
            checks.append(Check("fail", "database", "store is unreachable"))
    if skip_model:
        checks.append(Check("skip", "model host", "--skip-model"))
    elif not settings.model_base_url:
        checks.append(Check("fail", "model host", "OPENAI_BASE_URL is required"))
    else:
        try:
            fetch_model_ids(settings.model_base_url, settings.model_api_key_overwrite)
            checks.append(Check("ok", "model host", settings.model_base_url))
        except ConfigError as exc:
            checks.append(Check("fail", "model host", str(exc)))
    backend = load_isolation(settings.run_mode)
    try:
        require_run_mode(settings.run_mode, settings)
        detail = backend.name
        if backend.warn_not_production:
            detail = f"{backend.name} (not for production)"
        checks.append(Check("ok", "run mode", detail))
    except ConfigError as exc:
        checks.append(Check("fail", "run mode", str(exc)))
        return checks
    if fast or not backend.needs_probe:
        if fast and backend.needs_probe:
            checks.append(Check("skip", "sandbox probe", "--fast"))
    else:
        try:
            probe_run_mode(settings)
            checks.append(Check("ok", "sandbox probe", backend.name))
        except ConfigError as exc:
            checks.append(Check("fail", "sandbox probe", str(exc)))
    if settings.auth:
        try:
            load_authenticate(settings.auth)
            checks.append(Check("ok", "auth", settings.auth))
        except ConfigError as exc:
            checks.append(Check("fail", "auth", str(exc)))
    else:
        checks.append(Check("skip", "auth", "default hash"))
    return checks


def check_ready(
    *,
    config_path: str | None = None,
    skip_db: bool = False,
    skip_model: bool = False,
    fast: bool = False,
    out: TextIO | None = None,
) -> int:
    stream: TextIO = sys.stdout if out is None else out
    try:
        settings = load_settings(config_path=config_path)
    except ConfigError as exc:
        print(_line(Check("fail", "config", str(exc))), file=stream)
        return 1
    checks = run_checks(settings, skip_db=skip_db, skip_model=skip_model, fast=fast)
    failed = False
    for item in checks:
        print(_line(item), file=stream)
        if item.status == "fail":
            failed = True
    return 1 if failed else 0
