import asyncio
import os
import signal
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from tests.support.procs import fake_pi_shim, free_port

from apipi.dev import STOP_GRACE
from apipi.store.engine import Store, create_engine
from apipi.store.models import WorkerRow

pytestmark = pytest.mark.e2e


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _children(pid: int) -> list[int]:
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == pid:
            found.append(int(entry.name))
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1]
    except OSError:
        return False
    return state.split()[0] != "Z"


async def _wait_for(check, timeout: float, what: str) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await check():
            return
        await asyncio.sleep(0.1)
    raise TimeoutError(what)


class DevProcess:
    def __init__(self, proc: asyncio.subprocess.Process, cwd: Path, port: int) -> None:
        self.proc = proc
        self.cwd = cwd
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"


@pytest.fixture
async def dev(tmp_path: Path) -> AsyncIterator[DevProcess]:
    cwd = tmp_path / "project"
    cwd.mkdir()
    port = free_port()
    shim = fake_pi_shim(tmp_path)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("APIPI_") and key != "DATABASE_URL"
    }
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{cwd / 'dev.db'}",
            "APIPI_LOG_LEVEL": "warning",
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "APIPI_MODEL_LIST": "off",
            "APIPI_MODELS": '["test"]',
            "APIPI_PI_COMMAND": str(shim),
            "APIPI_WORKER_LEASE_TTL": "2s",
        }
    )
    log = (tmp_path / "dev.log").open("wb")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "apipi",
        "dev",
        "--port",
        str(port),
        cwd=cwd,
        env=env,
        stdout=log,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        yield DevProcess(proc, cwd, port)
    finally:
        if proc.returncode is None:
            proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=40)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        log.close()
        print((tmp_path / "dev.log").read_text(errors="replace")[-4000:])


async def _ready(dev: DevProcess) -> None:
    engine = create_engine(f"sqlite:///{dev.cwd / 'dev.db'}")
    store = Store(engine)

    async def registered() -> bool:
        if dev.proc.returncode is not None:
            raise RuntimeError("apipi dev exited early")
        try:
            async with AsyncClient(base_url=dev.base_url) as http:
                if (await http.get("/health")).status_code != 200:
                    return False
            async with store.session() as db:
                count = await db.scalar(select(func.count()).select_from(WorkerRow))
        except Exception:
            return False
        return bool(count)

    try:
        await _wait_for(registered, 60, "worker did not register")
    finally:
        await store.dispose()


async def test_dev_completes_a_turn_and_sigint_stops_both(dev: DevProcess) -> None:
    await _ready(dev)
    children = _children(dev.proc.pid)
    assert len(children) == 2
    token_file = dev.cwd / ".apipi" / "dev-worker-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert (dev.cwd / ".apipi" / "store").is_dir()
    token = "dev-user"
    async with AsyncClient(base_url=dev.base_url, timeout=30) as client:
        agent = await client.post(
            "/v1/agents", headers=_auth(token), json={"name": "bot", "model": "test"}
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
                "input": "hello-dev",
            },
        )
        assert created.status_code == 200
        events = await client.get(
            f"/v1/agents/sessions/{created.json()['id']}/events",
            headers=_auth(token),
        )
    types = [event["type"] for event in events.json()["data"]]
    assert "agent.session.turn.completed" in types
    dev.proc.send_signal(signal.SIGINT)
    await asyncio.wait_for(dev.proc.wait(), timeout=STOP_GRACE - 5)
    assert not any(_alive(pid) for pid in children)


@pytest.mark.parametrize("command", [b"worker", b"serve"], ids=["worker", "api"])
async def test_child_exit_stops_the_other(dev: DevProcess, command: bytes) -> None:
    await _ready(dev)
    children = _children(dev.proc.pid)
    assert len(children) == 2
    child = next(
        pid
        for pid in children
        if command in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    )
    os.kill(child, signal.SIGKILL)
    await asyncio.wait_for(dev.proc.wait(), timeout=30)
    assert not any(_alive(pid) for pid in children)
