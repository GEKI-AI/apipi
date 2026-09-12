import argparse
import asyncio
import logging
import sys

import uvicorn

from apipi.app import create_app
from apipi.config import (
    HOST_MODE_WARNING,
    ConfigError,
    Settings,
    load_settings,
    postgres_url,
    require_run_mode,
)
from apipi.store.engine import Store, create_engine
from apipi.store.migrate import migrate
from apipi.tenants import provision_tenant

log = logging.getLogger("apipi")


def prepare_serve(settings: Settings | None = None) -> Settings:
    resolved = settings if settings is not None else load_settings()
    postgres_url(resolved.database_url)
    require_run_mode(resolved.run_mode)
    if resolved.run_mode == "host":
        log.warning(HOST_MODE_WARNING)
    return resolved


def serve(*, host: str, port: int) -> None:
    settings = prepare_serve()
    uvicorn.run(create_app(settings), host=host, port=port)


async def _tenant_create(name: str) -> str:
    settings = load_settings()
    store = Store(create_engine(postgres_url(settings.database_url)))
    try:
        async with store.session() as db:
            _tenant, token = await provision_tenant(db, name=name)
        return token
    finally:
        await store.dispose()


def tenant_create(*, name: str) -> str:
    return asyncio.run(_tenant_create(name))


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
    tenant_parser = sub.add_parser("tenant", help="Tenants")
    tenant_sub = tenant_parser.add_subparsers(dest="tenant_command", required=True)
    create_tenant_parser = tenant_sub.add_parser(
        "create", help="Create a tenant and print a bearer token"
    )
    create_tenant_parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            migrate()
            return 0
        if args.command == "serve":
            serve(host=args.host, port=args.port)
            return 0
        if args.command == "tenant" and args.tenant_command == "create":
            print(tenant_create(name=args.name))
            return 0
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 1
