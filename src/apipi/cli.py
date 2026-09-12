import argparse
import logging
import sys

import uvicorn

from apipi.app import create_app
from apipi.config import (
    HOST_MODE_WARNING,
    METRICS_OFF,
    METRICS_ON,
    OTEL_SET,
    OTEL_UNSET,
    TURN_LOG_ON,
    ConfigError,
    Settings,
    load_settings,
    postgres_url,
    reject_prompt_body_logging,
    require_run_mode,
)
from apipi.store.migrate import migrate

log = logging.getLogger("apipi")


def prepare_serve(settings: Settings | None = None) -> Settings:
    resolved = settings if settings is not None else load_settings()
    postgres_url(resolved.database_url)
    require_run_mode(resolved.run_mode)
    reject_prompt_body_logging()
    if resolved.run_mode == "host":
        log.warning(HOST_MODE_WARNING)
    log.info(TURN_LOG_ON)
    log.info(METRICS_ON if resolved.metrics else METRICS_OFF)
    log.info(OTEL_SET if resolved.otel_endpoint else OTEL_UNSET)
    return resolved


def serve(*, host: str, port: int) -> None:
    settings = prepare_serve()
    uvicorn.run(create_app(settings), host=host, port=port)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(prog="apipi")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="Apply store migrations")
    serve_parser = sub.add_parser("serve", help="Start the API")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    serve_parser.add_argument("--port", default=8000, type=int, help="Bind port")
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            migrate()
            return 0
        if args.command == "serve":
            serve(host=args.host, port=args.port)
            return 0
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 1
