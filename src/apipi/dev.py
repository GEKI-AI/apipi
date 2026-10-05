import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

from apipi.config import (
    Settings,
    _toml_values,
    load_settings,
    resolve_config_path,
)
from apipi.services.worker_tokens import authenticate_token, create_token
from apipi.store.engine import Store, create_engine
from apipi.store.migrate import migrate

log = logging.getLogger("apipi")

DEV_DIR = Path(".apipi")
TOKEN_FILE = "dev-worker-token"
STOP_GRACE = 15.0


async def ensure_dev_token(settings: Settings, path: Path) -> Path:
    store = Store(create_engine(settings.database_url, pool_size=settings.db_pool_size))
    try:
        if path.is_file():
            secret = path.read_text().strip()
            if secret and await authenticate_token(store, secret) is not None:
                path.chmod(0o600)
                return path
        created = await create_token(store, name="apipi dev")
    finally:
        await store.dispose()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(created.secret)
    return path


def _client_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host


def _worker_takes_config(config_path: str | None) -> bool:
    path = resolve_config_path(config_path)
    return path is None or "database_url" not in _toml_values(path)


def child_commands(
    *, config_path: str | None, host: str, port: int
) -> tuple[list[str], list[str]]:
    base = [sys.executable, "-m", "apipi"]
    config = ["--config", config_path] if config_path else []
    api = [*base, "serve", "--host", host, "--port", str(port), *config]
    worker = [*base, "worker", *(config if _worker_takes_config(config_path) else [])]
    return api, worker


def child_environments(
    token_file: Path, *, host: str, port: int
) -> tuple[dict[str, str], dict[str, str]]:
    api = dict(os.environ)
    worker = dict(api)
    worker.pop("DATABASE_URL", None)
    worker["APIPI_API_URL"] = f"http://{_client_host(host)}:{port}"
    worker["APIPI_WORKER_TOKEN_FILE"] = str(token_file.resolve())
    worker.setdefault("APIPI_RUN_MODE", "none")
    return api, worker


def _stop(procs: list[subprocess.Popen[bytes]]) -> None:
    for proc in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 0
    return 128 - returncode if returncode < 0 else returncode


class _Stop(Exception):
    pass


def run_dev(*, config_path: str | None, host: str, port: int) -> int:
    migrate(config_path=config_path)
    settings = load_settings(config_path=config_path)
    token_file = asyncio.run(ensure_dev_token(settings, DEV_DIR / TOKEN_FILE))
    api_cmd, worker_cmd = child_commands(config_path=config_path, host=host, port=port)
    api_env, worker_env = child_environments(token_file, host=host, port=port)

    def _raise_stop(_signum: int, _frame: FrameType | None) -> None:
        raise _Stop

    previous = signal.signal(signal.SIGTERM, _raise_stop)
    procs: list[subprocess.Popen[bytes]] = []
    code = 0
    try:
        procs.append(subprocess.Popen(api_cmd, env=api_env))
        procs.append(subprocess.Popen(worker_cmd, env=worker_env))
        log.info(
            "dev",
            extra={"api_pid": procs[0].pid, "worker_pid": procs[1].pid, "port": port},
        )
        while True:
            exited = [proc for proc in procs if proc.poll() is not None]
            if exited:
                code = _exit_code(exited[0].returncode)
                break
            time.sleep(0.2)
    except (KeyboardInterrupt, _Stop):
        code = 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        _stop(procs)
    return code
