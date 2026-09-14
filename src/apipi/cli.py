import argparse
import logging
import os
import sys

import uvicorn

from apipi.app import create_app
from apipi.config import (
    METRICS_OFF,
    METRICS_ON,
    NONE_MODE_WARNING,
    OPENAI_API_KEY_IGNORED,
    OTEL_SET,
    OTEL_UNSET,
    PAYLOAD_EXPORT_OFF,
    PAYLOAD_EXPORT_ON,
    SQLITE_WARNING,
    USAGE_EXPORT_OFF,
    USAGE_EXPORT_ON,
    ConfigError,
    Settings,
    is_sqlite_url,
    load_settings,
    reject_prompt_body_logging,
    require_run_mode,
    usage_retention_log,
    usage_store_log,
)
from apipi.pi.install import install_pi
from apipi.pi.isolation import load_isolation
from apipi.pi.model_host import probe_model_host
from apipi.pi.probe import probe_run_mode
from apipi.ready import check_ready
from apipi.store.migrate import migrate

log = logging.getLogger("apipi")


def prepare_serve(
    settings: Settings | None = None, *, config_path: str | None = None
) -> Settings:
    resolved = (
        settings if settings is not None else load_settings(config_path=config_path)
    )
    require_run_mode(resolved.run_mode, resolved)
    if is_sqlite_url(resolved.database_url):
        log.warning(SQLITE_WARNING)
    probe_model_host(resolved)
    probe_run_mode(resolved)
    reject_prompt_body_logging()
    logging.getLogger().setLevel(resolved.log_level.upper())
    backend = load_isolation(resolved.run_mode)
    if os.environ.get("OPENAI_API_KEY"):
        log.warning(OPENAI_API_KEY_IGNORED)
    if backend.warn_not_production:
        if backend.name == "none":
            log.warning(NONE_MODE_WARNING)
        else:
            log.warning(f"APIPI_RUN_MODE={backend.name} is not suited for production")
    log.info(usage_store_log(resolved.usage_store))
    log.info(usage_retention_log(resolved.usage_retention))
    log.info(USAGE_EXPORT_ON if resolved.usage_export_url else USAGE_EXPORT_OFF)
    log.info(PAYLOAD_EXPORT_ON if resolved.payload_export_url else PAYLOAD_EXPORT_OFF)
    log.info(METRICS_ON if resolved.metrics else METRICS_OFF)
    log.info(OTEL_SET if resolved.otel_endpoint else OTEL_UNSET)
    return resolved


def serve(
    *, host: str | None, port: int | None, config_path: str | None = None
) -> None:
    settings = prepare_serve(config_path=config_path)
    uvicorn.run(
        create_app(settings),
        host=host if host is not None else settings.host,
        port=port if port is not None else settings.port,
        log_level=settings.log_level,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="apipi")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_parser = sub.add_parser("migrate", help="Apply store migrations")
    migrate_parser.add_argument("--config", default=None, help="TOML config file")
    install_parser = sub.add_parser("install", help="Install pinned Pi")
    install_parser.add_argument("--config", default=None, help="TOML config file")
    install_parser.add_argument(
        "--force", action="store_true", help="Reinstall even if Pi already matches"
    )
    install_parser.add_argument(
        "--dry-run", action="store_true", help="Print the npm command and exit"
    )
    check_parser = sub.add_parser("check", help="Verify requirements without serving")
    check_parser.add_argument("--config", default=None, help="TOML config file")
    check_parser.add_argument("--skip-db", action="store_true", help="Skip the store")
    check_parser.add_argument(
        "--skip-model", action="store_true", help="Skip the model host"
    )
    check_parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip the throwaway sandbox probe",
    )
    serve_parser = sub.add_parser("serve", help="Start the API")
    serve_parser.add_argument("--host", default=None, help="Bind address")
    serve_parser.add_argument("--port", default=None, type=int, help="Bind port")
    serve_parser.add_argument("--config", default=None, help="TOML config file")
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            migrate(config_path=args.config)
            return 0
        if args.command == "install":
            settings = load_settings(config_path=args.config)
            return install_pi(settings, force=args.force, dry_run=args.dry_run)
        if args.command == "check":
            return check_ready(
                config_path=args.config,
                skip_db=args.skip_db,
                skip_model=args.skip_model,
                fast=args.fast,
            )
        if args.command == "serve":
            serve(host=args.host, port=args.port, config_path=args.config)
            return 0
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 1
