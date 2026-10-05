"""Real `apipi serve` + `apipi worker` subprocesses for e2e tests.

One helper for every process-level e2e test: an ephemeral loopback port
(no fixed ports), tmp dirs only, the test's own `Store` database shared
with the API process, and a fake Pi on the worker. The worker holds no
database credentials, exactly as in production.
"""

import asyncio
import contextlib
import os
import socket
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select

from apipi.services.worker_tokens import create_token
from apipi.store.engine import Store
from apipi.store.models import WorkerRow
from apipi.worker.pi.version import PINNED_PI

FAKE_PI = Path(__file__).resolve().parent / "fake_pi.py"


@dataclass
class SplitProcesses:
    base_url: str
    api: asyncio.subprocess.Process
    worker: asyncio.subprocess.Process
    api_log: Path
    worker_log: Path
    worker_sessions: Path


def _tail(api_log: Path, worker_log: Path) -> str:
    parts = []
    for name, path in (("api", api_log), ("worker", worker_log)):
        text = path.read_text(errors="replace") if path.exists() else ""
        parts.append(f"--- {name} ---\n{text[-4000:]}")
    return "\n".join(parts)


def fake_pi_shim(directory: Path) -> Path:
    shim = directory / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        f'[ "$1" = "--version" ] && {{ echo {PINNED_PI}; exit 0; }}\n'
        f'exec "{sys.executable}" "{FAKE_PI}" "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_ready(
    procs: tuple[asyncio.subprocess.Process, ...],
    store: Store,
    base_url: str,
    logs: str,
    timeout: float,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    async with AsyncClient(base_url=base_url) as http:
        while asyncio.get_running_loop().time() < deadline:
            for proc in procs:
                if proc.returncode is not None:
                    raise RuntimeError(f"process exited early: {logs}")
            try:
                if (await http.get("/health")).status_code == 200:
                    async with store.session() as db:
                        count = await db.scalar(
                            select(func.count()).select_from(WorkerRow)
                        )
                    if count:
                        return
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"split processes not ready: {logs}")


async def _stop(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except TimeoutError:
        proc.kill()
        await proc.wait()


@contextlib.asynccontextmanager
async def split_processes(
    store: Store,
    tmp_path: Path,
    *,
    env: dict[str, str] | None = None,
    api_env: dict[str, str] | None = None,
    worker_env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> AsyncIterator[SplitProcesses]:
    """Start the API and one worker as subprocesses; stop both on exit."""
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    created = await create_token(store, name="e2e-worker")
    token_file = tmp_path / "worker.token"
    token_file.write_text(created.secret)
    token_file.chmod(0o600)
    database_url = store.engine.url.render_as_string(hide_password=False)
    # The worker probes `<pi> --version` against the pinned release, so the
    # fake Pi is wrapped in a shim that answers it.
    shim = fake_pi_shim(tmp_path)
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("APIPI_") and key != "DATABASE_URL"
    }
    base.update(
        {
            "APIPI_RUN_MODE": "none",
            "APIPI_LOG_LEVEL": "warning",
            # The fake Pi never calls a model; skip the /models probe.
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "APIPI_MODEL_LIST": "off",
            "APIPI_MODELS": '["test"]',
            "APIPI_PI_COMMAND": str(shim),
            "APIPI_LOCAL_STORE_DIR": str(tmp_path / "blobs"),
            **(env or {}),
        }
    )
    api = {
        **base,
        "DATABASE_URL": database_url,
        "APIPI_HOST": "127.0.0.1",
        "APIPI_PORT": str(port),
        "APIPI_SESSIONS_DIR": str(tmp_path / "api-sessions"),
        **(api_env or {}),
    }
    worker = {
        **base,
        "APIPI_API_URL": base_url,
        "APIPI_WORKER_TOKEN_FILE": str(token_file),
        "APIPI_SESSIONS_DIR": str(tmp_path / "worker-sessions"),
        "APIPI_WORKER_OUTBOX_DIR": str(tmp_path / "outbox"),
        # The worker notices SIGTERM once per heartbeat (lease_ttl / 2).
        "APIPI_WORKER_LEASE_TTL": "2s",
        **(worker_env or {}),
    }
    api_log = tmp_path / "api.log"
    worker_log = tmp_path / "worker.log"
    procs: list[asyncio.subprocess.Process] = []
    try:
        with api_log.open("wb") as api_out, worker_log.open("wb") as worker_out:
            for args, proc_env, out in (
                (("serve",), api, api_out),
                (("worker", "--drain-timeout", "0.5"), worker, worker_out),
            ):
                procs.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "apipi",
                        *args,
                        env=proc_env,
                        stdout=out,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                )
            logs = f"{api_log}, {worker_log}"
            await _wait_ready(tuple(procs), store, base_url, logs, timeout)
            yield SplitProcesses(
                base_url,
                procs[0],
                procs[1],
                api_log,
                worker_log,
                tmp_path / "worker-sessions",
            )
    finally:
        for proc in reversed(procs):
            await _stop(proc)
        print(_tail(api_log, worker_log))


@contextlib.asynccontextmanager
async def split_http_client(
    store: Store, tmp_path: Path, *, http_timeout: float = 30.0, **kwargs: Any
) -> AsyncIterator[AsyncClient]:
    """`split_processes` plus an HTTP client pointed at the API process."""
    async with (
        split_processes(store, tmp_path, **kwargs) as procs,
        AsyncClient(base_url=procs.base_url, timeout=http_timeout) as client,
    ):
        yield client
