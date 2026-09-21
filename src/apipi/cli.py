import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from apipi import __version__
from apipi.config import (
    CHAT_MODE_NOTE,
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
    VAULT_MASTER_KEY_UNSET,
    ConfigError,
    Settings,
    is_sqlite_url,
    load_settings,
    reject_prompt_body_logging,
    require_run_mode,
    usage_retention_log,
    usage_store_log,
)
from apipi.gateway import create_app
from apipi.gateway.logutil import configure_logging, uvicorn_log_config
from apipi.gateway.ready import check_ready
from apipi.services.vault_crypto import vault_master_key_unset
from apipi.store.migrate import migrate
from apipi.worker.pi.install import run_install
from apipi.worker.pi.isolation import load_isolation
from apipi.worker.pi.microvm import (
    SHELL_WARNING,
    microvm_shell_needs_sudo,
    reexec_microvm_shell,
    run_microvm_shell,
)
from apipi.worker.pi.model_host import probe_model_host
from apipi.worker.pi.probe import probe_run_mode

log = logging.getLogger("apipi")


def prepare_serve(
    settings: Settings | None = None,
    *,
    config_path: str | None = None,
    api_only: bool = False,
) -> Settings:
    resolved = (
        settings if settings is not None else load_settings(config_path=config_path)
    )
    if api_only and not resolved.api_only:
        resolved = resolved.model_copy(update={"api_only": True})
    if not resolved.api_only:
        require_run_mode(resolved.run_mode, resolved)
    if is_sqlite_url(resolved.database_url):
        log.warning(SQLITE_WARNING)
    probe_model_host(resolved)
    if not resolved.api_only:
        probe_run_mode(resolved)
    reject_prompt_body_logging()
    configure_logging(level=resolved.log_level, format=resolved.log_format)
    backend = load_isolation(resolved.run_mode)
    if os.environ.get("OPENAI_API_KEY"):
        log.warning(OPENAI_API_KEY_IGNORED)
    if backend.name == "chat":
        log.info(CHAT_MODE_NOTE)
    elif backend.warn_not_production:
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


def prepare_worker(
    settings: Settings | None = None, *, config_path: str | None = None
) -> Settings:
    resolved = (
        settings if settings is not None else load_settings(config_path=config_path)
    )
    if resolved.worker_token is None or resolved.worker_token == "":
        raise ConfigError("APIPI_WORKER_TOKEN is required")
    if vault_master_key_unset(resolved.vault_master_key):
        log.warning(VAULT_MASTER_KEY_UNSET)
    require_run_mode(resolved.run_mode, resolved)
    probe_run_mode(resolved)
    reject_prompt_body_logging()
    configure_logging(level=resolved.log_level, format=resolved.log_format)
    backend = load_isolation(resolved.run_mode)
    if backend.name == "chat":
        log.info(CHAT_MODE_NOTE)
    elif backend.warn_not_production:
        if backend.name == "none":
            log.warning(NONE_MODE_WARNING)
        else:
            log.warning(f"APIPI_RUN_MODE={backend.name} is not suited for production")
    log.info("worker sandbox", extra={"run_mode": backend.name})
    return resolved


def microvm_shell(
    *,
    config_path: str | None,
    image: str | None,
    workspace: str | None,
) -> int:
    if not sys.stdin.isatty():
        print(
            "apipi microvm shell needs a TTY. Run it in a terminal, not a pipe.",
            file=sys.stderr,
        )
        return 1
    extra: list[str] = []
    if config_path is not None:
        extra.extend(["--config", config_path])
    if image is not None:
        extra.extend(["--image", image])
    if workspace is not None:
        extra.extend(["--workspace", workspace])
    if microvm_shell_needs_sudo():
        reexec_microvm_shell(extra)
        return 0
    settings = load_settings(config_path=config_path)
    configure_logging(level=settings.log_level, format=settings.log_format)
    if image is not None:
        settings = settings.model_copy(update={"microvm_image": image})
    print(SHELL_WARNING, file=sys.stderr)
    return asyncio.run(run_microvm_shell(settings, cwd=workspace))


def serve(
    *,
    host: str | None,
    port: int | None,
    config_path: str | None = None,
    api_only: bool = False,
) -> None:
    settings = prepare_serve(config_path=config_path, api_only=api_only)
    host = host if host is not None else settings.host
    port = port if port is not None else settings.port
    extra: dict[str, object] = {
        "version": __version__,
        "host": host,
        "port": port,
        "run_mode": settings.run_mode,
        "store": "sqlite" if is_sqlite_url(settings.database_url) else "postgres",
        "role": "api" if api_only else "all",
    }
    if settings.instance_id:
        extra["instance_id"] = settings.instance_id
    log.info("serve", extra=extra)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level=settings.log_level,
        access_log=False,
        log_config=uvicorn_log_config(
            level=settings.log_level, format=settings.log_format
        ),
    )


def main(argv: list[str] | None = None) -> int:
    level = os.environ.get("APIPI_LOG_LEVEL", "info").lower()
    fmt = os.environ.get("APIPI_LOG_FORMAT", "json").lower()
    if level not in {"debug", "info", "warning", "error", "critical"}:
        level = "info"
    if fmt not in {"json", "text"}:
        fmt = "json"
    configure_logging(level=level, format=fmt)
    parser = argparse.ArgumentParser(prog="apipi")
    sub = parser.add_subparsers(dest="command", required=True)
    migrate_parser = sub.add_parser("migrate", help="Apply store migrations")
    migrate_parser.add_argument("--config", default=None, help="TOML config file")
    install_parser = sub.add_parser("install", help="Install Pi and/or MicroVM")
    install_parser.add_argument("--config", default=None, help="TOML config file")
    install_parser.add_argument("--pi", action="store_true", help="Install pinned Pi")
    install_parser.add_argument(
        "--microvm",
        action="store_true",
        help="Install Firecracker, jailer, and guest images",
    )
    install_parser.add_argument(
        "--image",
        choices=("default", "browser"),
        default=None,
        help="MicroVM rootfs flavor (default: default)",
    )
    install_parser.add_argument(
        "--force", action="store_true", help="Reinstall even if already present"
    )
    install_parser.add_argument(
        "--dry-run", action="store_true", help="Print install commands and exit"
    )
    install_parser.add_argument(
        "--role",
        choices=("api", "worker", "all"),
        default="all",
        help="What this host will run (default: all)",
    )
    check_parser = sub.add_parser("check", help="Verify requirements without serving")
    check_parser.add_argument("--config", default=None, help="TOML config file")
    check_parser.add_argument(
        "--role",
        choices=("api", "worker", "all"),
        default="all",
        help="What this host will run (default: all)",
    )
    check_parser.add_argument("--skip-db", action="store_true", help="Skip the store")
    check_parser.add_argument(
        "--skip-model", action="store_true", help="Skip the model host"
    )
    check_parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip the throwaway sandbox probe",
    )
    serve_parser = sub.add_parser(
        "serve", help="Start the API (combined with a local sandbox by default)"
    )
    serve_parser.add_argument("--host", default=None, help="Bind address")
    serve_parser.add_argument("--port", default=None, type=int, help="Bind port")
    serve_parser.add_argument("--config", default=None, help="TOML config file")
    serve_parser.add_argument(
        "--api-only",
        action="store_true",
        help="Control plane only: no KVM probe and no local Firecracker",
    )
    worker_parser = sub.add_parser(
        "worker", help="Start a sandbox worker (Firecracker/KVM lives here)"
    )
    worker_parser.add_argument("--config", default=None, help="TOML config file")
    worker_parser.add_argument(
        "--url",
        default=None,
        help="API base URL (default: APIPI_API_URL or http://127.0.0.1:8000)",
    )
    worker_parser.add_argument(
        "--drain-timeout",
        default=None,
        type=float,
        help="Seconds to wait after SIGTERM for live Pi to empty (default: idle TTL)",
    )
    microvm_parser = sub.add_parser("microvm", help="Operator microVM tools")
    microvm_sub = microvm_parser.add_subparsers(dest="microvm_command", required=True)
    shell_parser = microvm_sub.add_parser(
        "shell", help="Boot a guest and attach a serial shell"
    )
    shell_parser.add_argument("--config", default=None, help="TOML config file")
    shell_parser.add_argument(
        "--image",
        choices=("default", "browser"),
        default=None,
        help="Rootfs flavor (default: APIPI_MICROVM_IMAGE)",
    )
    shell_parser.add_argument(
        "--workspace",
        default=None,
        help="Host directory packed into guest /workspace",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "migrate":
            migrate(config_path=args.config)
            return 0
        if args.command == "install":
            settings = load_settings(config_path=args.config)
            configure_logging(level=settings.log_level, format=settings.log_format)
            flagged = args.pi or args.microvm or args.image is not None
            if args.role == "api" and not flagged:
                print("API role needs no Pi or MicroVM install")
                return 0
            if args.role == "worker" and not flagged:
                flagged = True
                args.microvm = True
            return run_install(
                settings,
                pi=args.pi if flagged else None,
                microvm=(args.microvm or args.image is not None) if flagged else None,
                image=args.image,
                force=args.force,
                dry_run=args.dry_run,
            )
        if args.command == "check":
            return check_ready(
                config_path=args.config,
                skip_db=args.skip_db,
                skip_model=args.skip_model,
                fast=args.fast,
                role=args.role,
            )
        if args.command == "serve":
            serve(
                host=args.host,
                port=args.port,
                config_path=args.config,
                api_only=args.api_only,
            )
            return 0
        if args.command == "worker":
            from apipi.worker.hub import run_worker

            settings = prepare_worker(config_path=args.config)
            return asyncio.run(
                run_worker(
                    settings,
                    url=args.url,
                    drain_timeout=args.drain_timeout,
                )
            )
        if args.command == "microvm" and args.microvm_command == "shell":
            return microvm_shell(
                config_path=args.config,
                image=args.image,
                workspace=args.workspace,
            )
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 1
