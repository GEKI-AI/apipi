import asyncio
import os
import sys
from dataclasses import dataclass
from typing import TextIO

import asyncpg

from apipi import __version__
from apipi.auth import load_authenticate
from apipi.config import (
    ConfigError,
    Settings,
    load_settings,
    postgres_url,
    require_run_mode,
)
from apipi.pi.isolation import load_isolation
from apipi.pi.model_host import fetch_model_ids, installed_pi_version, require_pinned_pi
from apipi.pi.probe import probe_run_mode
from apipi.pi.version import PINNED_PI


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str = ""


def _line(check: Check) -> str:
    extra = f"  {check.detail}" if check.detail else ""
    return f"{check.status:<4} {check.name}{extra}"


def ping_postgres(url: str) -> None:
    dsn = postgres_url(url).replace("postgresql+asyncpg://", "postgresql://", 1)

    async def _ping() -> None:
        conn = await asyncpg.connect(dsn=dsn, timeout=5)
        try:
            await conn.execute("SELECT 1")
        finally:
            await conn.close()

    asyncio.run(_ping())


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
    if skip_db:
        checks.append(Check("skip", "database", "--skip-db"))
    else:
        try:
            postgres_url(settings.database_url)
            ping_postgres(settings.database_url)
            checks.append(Check("ok", "database", "reachable"))
        except ConfigError as exc:
            checks.append(Check("fail", "database", str(exc)))
        except (OSError, asyncpg.PostgresError, TimeoutError):
            checks.append(Check("fail", "database", "Postgres is unreachable"))
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
        if skip_db and str(exc) == "DATABASE_URL is required":
            os.environ["DATABASE_URL"] = (
                "postgresql+asyncpg://apipi:apipi@127.0.0.1:1/apipi"
            )
            try:
                settings = load_settings(config_path=config_path)
            except ConfigError as retry:
                print(_line(Check("fail", "config", str(retry))), file=stream)
                return 1
        else:
            print(_line(Check("fail", "config", str(exc))), file=stream)
            return 1
    checks = run_checks(settings, skip_db=skip_db, skip_model=skip_model, fast=fast)
    failed = False
    for item in checks:
        print(_line(item), file=stream)
        if item.status == "fail":
            failed = True
    return 1 if failed else 0
