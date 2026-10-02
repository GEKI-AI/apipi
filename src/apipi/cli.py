import argparse
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import uvicorn

from apipi import __version__
from apipi.config import (
    LIFECYCLE_EXPORT_OFF,
    LIFECYCLE_EXPORT_ON,
    METRICS_OFF,
    METRICS_ON,
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
    load_worker_token,
    reject_legacy_worker_token,
    reject_prompt_body_logging,
    reject_worker_database_url,
    reject_worker_database_url_toml,
    require_run_mode,
    usage_retention_log,
    usage_store_log,
)
from apipi.gateway import create_app
from apipi.gateway.logutil import configure_logging, uvicorn_log_config
from apipi.gateway.ready import check_ready
from apipi.store.engine import Store
from apipi.store.migrate import migrate
from apipi.worker.pi.image_check import (
    image_check_needs_sudo,
    reexec_image_check,
    run_image_check,
)
from apipi.worker.pi.image_ops import build_image, publish_images
from apipi.worker.pi.image_pull import list_images, pull_images
from apipi.worker.pi.image_store import open_image_store
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
    settings: Settings | None = None, *, config_path: str | None = None
) -> Settings:
    resolved = (
        settings if settings is not None else load_settings(config_path=config_path)
    )
    if is_sqlite_url(resolved.database_url):
        log.warning(SQLITE_WARNING)
    probe_model_host(resolved)
    reject_prompt_body_logging()
    from apipi.worker.pi.fragments import validate_fragments

    validate_fragments(resolved)
    configure_logging(level=resolved.log_level, format=resolved.log_format)
    if os.environ.get("OPENAI_API_KEY"):
        log.warning(OPENAI_API_KEY_IGNORED)
    log.info(usage_store_log(resolved.usage_store))
    log.info(usage_retention_log(resolved.usage_retention))
    log.info(USAGE_EXPORT_ON if resolved.usage_export_url else USAGE_EXPORT_OFF)
    log.info(PAYLOAD_EXPORT_ON if resolved.payload_export_url else PAYLOAD_EXPORT_OFF)
    lifecycle_on = bool(
        resolved.lifecycle_export_url or resolved.lifecycle_sinks.strip()
    )
    log.info(LIFECYCLE_EXPORT_ON if lifecycle_on else LIFECYCLE_EXPORT_OFF)
    log.info(METRICS_ON if resolved.metrics else METRICS_OFF)
    log.info(OTEL_SET if resolved.otel_endpoint else OTEL_UNSET)
    _warn_model_retry(resolved)
    return resolved


def prepare_worker(
    settings: Settings | None = None, *, config_path: str | None = None
) -> Settings:
    if settings is None:
        reject_worker_database_url_toml(config_path)
        resolved = load_settings(config_path=config_path)
    else:
        resolved = settings
    reject_legacy_worker_token()
    reject_worker_database_url()
    load_worker_token(resolved.worker_token_file)
    # Workers never decrypt vaults: the API resolves MCP credentials into
    # turn contexts, so no vault master key warning belongs here.
    require_run_mode(resolved.run_mode, resolved)
    probe_model_host(resolved)
    probe_run_mode(resolved)
    reject_prompt_body_logging()
    from apipi.worker.pi.fragments import validate_fragments

    validate_fragments(resolved)
    configure_logging(level=resolved.log_level, format=resolved.log_format)
    backend = load_isolation(resolved.run_mode)
    if backend.warn_not_production:
        log.warning(f"APIPI_RUN_MODE={backend.name} is not suited for production")
    from apipi.worker.accepts import require_worker_accepts

    require_worker_accepts(resolved)
    log.info("worker sandbox", extra={"run_mode": backend.name})
    _warn_model_retry(resolved)
    return resolved


def _warn_model_retry(settings: Settings) -> None:
    from apipi.worker.pi.settings_json import model_retry_warnings

    for note in model_retry_warnings(settings):
        log.warning(note)


def _workers_token_command(args: argparse.Namespace) -> int:
    from apipi.services.worker_tokens import create_token, list_tokens
    from apipi.store.engine import create_engine

    settings = load_settings(config_path=args.config)
    store = Store(create_engine(settings.database_url, pool_size=settings.db_pool_size))
    try:
        if args.token_command == "create":
            worker_id = _parse_uuid(args.worker_id, "--worker-id")
            created = asyncio.run(
                create_token(store, name=args.name or "", worker_id=worker_id)
            )
            print(created.secret)
            print(
                f"created worker token {created.row.id} "
                f"({created.row.name or 'unnamed'})",
                file=sys.stderr,
            )
            return 0
        if args.token_command == "list":
            rows = asyncio.run(list_tokens(store))
            print("id\tname\tworker_id\tcreated\tlast_used\trevoked")
            for row in rows:
                print(
                    f"{row.id}\t{row.name}\t{row.worker_id or ''}"
                    f"\t{row.created_at.isoformat()}"
                    f"\t{row.last_used_at.isoformat() if row.last_used_at else ''}"
                    f"\t{row.revoked_at.isoformat() if row.revoked_at else ''}"
                )
            return 0
        if args.token_command == "revoke":
            target = asyncio.run(_revoke_worker_token(store, args.token))
            if target is None:
                print(f"unknown worker token: {args.token}", file=sys.stderr)
                return 1
            print(f"revoked worker token {target}", file=sys.stderr)
            return 0
    finally:
        asyncio.run(store.dispose())
    return 1


def _parse_uuid(raw: str | None, flag: str) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ConfigError(f"{flag} must be a UUID") from None


async def _revoke_worker_token(store: Store, raw: str) -> uuid.UUID | None:
    from apipi.services.worker_tokens import list_tokens as _list
    from apipi.services.worker_tokens import revoke_token as _revoke

    try:
        token_id = uuid.UUID(raw)
    except ValueError:
        token_id = None
    if token_id is not None:
        row = await _revoke(store, token_id)
        return row.id if row is not None else None
    rows = [row for row in await _list(store) if row.name == raw]
    if len(rows) != 1:
        return None
    revoked = await _revoke(store, rows[0].id)
    return revoked.id if revoked is not None else None


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
        settings = settings.model_copy(update={"sandbox_default_image": image})
    print(SHELL_WARNING, file=sys.stderr)
    return asyncio.run(run_microvm_shell(settings, cwd=workspace))


def _images_command(args: argparse.Namespace) -> int:
    if args.images_command == "pull":
        settings = load_settings(config_path=args.config)
        if args.source:
            settings = settings.model_copy(update={"image_source": args.source})
        for line in pull_images(settings, ids=list(args.ids) or None, force=args.force):
            print(line)
        return 0
    if args.images_command == "list":
        settings = load_settings(config_path=args.config)
        print("id\tversion\tdigest\tstatus")
        for image_id, version, digest, status in list_images(
            settings, remote=args.remote
        ):
            print(f"{image_id}\t{version}\t{digest}\t{status}")
        return 0
    if args.images_command == "build":
        out = Path(args.out) if args.out else _default_image_build_dir()
        manifest = build_image(args.id, out_dir=out, arch=args.arch)
        print(f"built {manifest.id} {manifest.version} at {out}")
        return 0
    if args.images_command == "push":
        return _images_push(args)
    if args.images_command == "check":
        return _images_check(args)
    if args.images_command == "mirror":
        return _images_mirror(args)
    if args.images_command == "verify":
        return _images_verify(args)
    return 1


def _images_check(args: argparse.Namespace) -> int:
    if args.id not in {"default", "browser", "work"}:
        raise ConfigError("apipi images check supports default, browser, and work")
    extra: list[str] = [args.id]
    if args.config is not None:
        extra.extend(["--config", args.config])
    if args.rootfs is not None:
        extra.extend(["--rootfs", args.rootfs])
    if args.boot:
        extra.append("--boot")
    if args.boot and image_check_needs_sudo():
        reexec_image_check(extra)
        return 0
    settings = load_settings(config_path=args.config)
    configure_logging(level=settings.log_level, format=settings.log_format)
    run_image_check(settings, args.id, rootfs=args.rootfs, boot=args.boot)
    return 0


def _images_mirror(args: argparse.Namespace) -> int:
    from apipi.worker.pi.image_catalog import mirror_store

    settings = load_settings(config_path=args.config)
    configure_logging(level=settings.log_level, format=settings.log_format)
    for line in mirror_store(
        settings,
        args.source,
        args.to,
        version=args.version,
        no_signature=args.no_signature,
        dry_run=args.dry_run,
        signer_identity=args.signer_identity,
        signer_issuer=args.signer_issuer,
    ):
        print(line)
    return 0


def _images_verify(args: argparse.Namespace) -> int:
    from apipi.worker.pi.image_catalog import verify_store

    settings = load_settings(config_path=args.config)
    configure_logging(level=settings.log_level, format=settings.log_format)
    verify_store(
        settings,
        source=args.source,
        version=args.version,
        local=args.local,
        no_signature=args.no_signature,
        signer_identity=args.signer_identity,
        signer_issuer=args.signer_issuer,
    )
    print("image store verify ok")
    return 0


def _images_push(args: argparse.Namespace) -> int:
    settings = load_settings(config_path=args.config)
    target = args.to or settings.image_source
    if not target:
        raise ConfigError("APIPI_IMAGE_SOURCE is unset. Set it, or pass --to.")
    source = Path(args.source) if args.source else _default_image_build_dir()
    if args.store_version:
        from apipi.worker.pi.image_catalog import join_store

        target = join_store(target, args.store_version)
    store = open_image_store(target, settings, write=True)
    planned = publish_images(
        store,
        source,
        ids=list(args.ids),
        force=args.force,
        dry_run=args.dry_run,
        store_version=args.store_version,
    )
    for name in planned:
        if name.startswith("skip "):
            log.info(name)
        print(name)
    return 0


def _add_image_push_parser(subparsers: Any, name: str, help_text: str) -> None:
    parser = subparsers.add_parser(name, help=help_text)
    parser.add_argument("--config", default=None, help="TOML config file")
    parser.add_argument(
        "--to",
        default=None,
        help="s3:// or file:// store (default: APIPI_IMAGE_SOURCE)",
    )
    parser.add_argument(
        "--from",
        dest="source",
        default=None,
        help="Build directory (default: last images build output)",
    )
    parser.add_argument("ids", nargs="*", help="Image ids to push")
    parser.add_argument(
        "--store-version",
        default=None,
        help="Write a versioned schema 2 prefix instead of a legacy store",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-upload an image that is already in the store",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print objects that would be uploaded",
    )


def _default_image_build_dir() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path.home() / ".cache"
    return base / "apipi" / "image-build"


def serve(
    *,
    host: str | None,
    port: int | None,
    config_path: str | None = None,
) -> None:
    settings = prepare_serve(config_path=config_path)
    host = host if host is not None else settings.host
    port = port if port is not None else settings.port
    extra: dict[str, object] = {
        "version": __version__,
        "host": host,
        "port": port,
        "store": "sqlite" if is_sqlite_url(settings.database_url) else "postgres",
        "role": "api",
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
        default=None,
        help="MicroVM image recipe id (default: sandbox_default_image)",
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
        choices=("api", "worker"),
        required=True,
        help="What this host will run",
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
    serve_parser = sub.add_parser("serve", help="Start the API")
    serve_parser.add_argument("--host", default=None, help="Bind address")
    serve_parser.add_argument("--port", default=None, type=int, help="Bind port")
    serve_parser.add_argument("--config", default=None, help="TOML config file")
    dev_parser = sub.add_parser(
        "dev", help="Migrate, then run the API and one worker for local development"
    )
    dev_parser.add_argument("--host", default="127.0.0.1", help="API bind address")
    dev_parser.add_argument("--port", default=8000, type=int, help="API bind port")
    dev_parser.add_argument("--config", default=None, help="TOML config file")
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
    workers_parser = sub.add_parser("workers", help="Manage sandbox workers")
    workers_parser.add_argument("--config", default=None, help="TOML config file")
    workers_sub = workers_parser.add_subparsers(dest="workers_command", required=True)
    token_parser = workers_sub.add_parser("token", help="Per-worker tokens")
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser(
        "create", help="Create a per-worker token (prints the secret once)"
    )
    token_create.add_argument("--name", default="", help="Token label")
    token_create.add_argument(
        "--worker-id",
        default=None,
        help="Bind the token to this worker id now (default: bind on first register)",
    )
    token_sub.add_parser("list", help="List per-worker tokens")
    token_revoke = token_sub.add_parser("revoke", help="Revoke a per-worker token")
    token_revoke.add_argument("token", help="Token id or exact name")
    microvm_parser = sub.add_parser("microvm", help="Operator microVM tools")
    microvm_sub = microvm_parser.add_subparsers(dest="microvm_command", required=True)
    shell_parser = microvm_sub.add_parser(
        "shell", help="Boot a guest and attach a serial shell"
    )
    shell_parser.add_argument("--config", default=None, help="TOML config file")
    shell_parser.add_argument(
        "--image",
        default=None,
        help="Image id (default: sandbox_default_image). browser is x86_64 only.",
    )
    shell_parser.add_argument(
        "--workspace",
        default=None,
        help="Host directory packed into guest /workspace",
    )
    images_parser = sub.add_parser("images", help="Build, push, and pull guest images")
    images_sub = images_parser.add_subparsers(dest="images_command", required=True)
    build_parser = images_sub.add_parser("build", help="Build one guest image")
    build_parser.add_argument("id", help="Recipe id")
    build_parser.add_argument("--out", default=None, help="Output directory")
    build_parser.add_argument(
        "--arch",
        default=None,
        help="Host arch check only; cross-build is not supported",
    )
    _add_image_push_parser(images_sub, "push", "Push built images to the image store")
    pull_parser = images_sub.add_parser("pull", help="Pull guest images")
    pull_parser.add_argument("--config", default=None, help="TOML config file")
    pull_parser.add_argument("ids", nargs="*", help="Image ids to pull")
    pull_parser.add_argument("--source", default=None, help="Image source URI")
    pull_parser.add_argument(
        "--force", action="store_true", help="Download even if the digest matches"
    )
    list_parser = images_sub.add_parser("list", help="List local and remote images")
    list_parser.add_argument("--config", default=None, help="TOML config file")
    list_parser.add_argument(
        "--remote", action="store_true", help="Compare with the image source"
    )
    mirror_parser = images_sub.add_parser(
        "mirror", help="Copy a versioned image store without root"
    )
    mirror_parser.add_argument("--config", default=None, help="TOML config file")
    mirror_parser.add_argument("--from", dest="source", required=True)
    mirror_parser.add_argument("--to", required=True)
    mirror_parser.add_argument("--version", default=None)
    mirror_parser.add_argument("--no-signature", action="store_true")
    mirror_parser.add_argument("--dry-run", action="store_true")
    mirror_parser.add_argument(
        "--signer-identity",
        default=None,
        help="Exact Sigstore identity. Defaults to the release-tag workflow.",
    )
    mirror_parser.add_argument("--signer-issuer", default=None)
    verify_parser = images_sub.add_parser(
        "verify", help="Check a store or the local images"
    )
    verify_parser.add_argument("--config", default=None, help="TOML config file")
    verify_parser.add_argument("--source", default=None)
    verify_parser.add_argument("--version", default=None)
    verify_parser.add_argument("--local", action="store_true")
    verify_parser.add_argument("--no-signature", action="store_true")
    verify_parser.add_argument(
        "--signer-identity",
        default=None,
        help="Exact Sigstore identity. Defaults to the release-tag workflow.",
    )
    verify_parser.add_argument("--signer-issuer", default=None)
    check_parser = images_sub.add_parser(
        "check", help="Check a built guest image on this machine"
    )
    check_parser.add_argument("id", help="Recipe id")
    check_parser.add_argument("--config", default=None, help="TOML config file")
    check_parser.add_argument(
        "--rootfs", default=None, help="rootfs-browser.ext4 to check"
    )
    check_parser.add_argument(
        "--boot",
        action="store_true",
        help="Also boot the image (needs KVM). Not for CI.",
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
            )
            return 0
        if args.command == "dev":
            from apipi.dev import run_dev

            return run_dev(config_path=args.config, host=args.host, port=args.port)
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
        if args.command == "workers" and args.workers_command == "token":
            return _workers_token_command(args)
        if args.command == "microvm" and args.microvm_command == "shell":
            return microvm_shell(
                config_path=args.config,
                image=args.image,
                workspace=args.workspace,
            )
        if args.command == "images":
            return _images_command(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 1
