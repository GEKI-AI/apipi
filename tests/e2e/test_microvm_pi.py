import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from tests.support.http import auth
from tests.support.microvm import image_paths, microvm_or_skip
from tests.support.procs import split_http_client

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.worker.pi.probe import probe_run_mode

pytestmark = [pytest.mark.e2e, pytest.mark.microvm, pytest.mark.timeout(300)]

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"
_GUEST_FAKE_PI = "/workspace/fake_pi.py"


@pytest.fixture
def microvm_settings(tmp_path: Path) -> Settings:
    kernel, rootfs = image_paths()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        idle_ttl=timedelta(seconds=30),
        pi_command=f"python3 {_GUEST_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
        microvm_kernel=kernel,
        microvm_rootfs=rootfs,
    )
    microvm_or_skip(settings)
    return settings


@pytest.fixture
async def microvm_client(
    microvm_settings: Settings, store: Store, tmp_path: Path
) -> AsyncIterator[AsyncClient]:
    """Real `apipi serve` + microvm `apipi worker` processes."""
    env = {
        "APIPI_RUN_MODE": "microvm",
        "APIPI_MICROVM_KERNEL": microvm_settings.microvm_kernel or "",
        "APIPI_MICROVM_ROOTFS": microvm_settings.microvm_rootfs or "",
        # Short hosted TTL: the worker reaps the guest by itself, since
        # the test cannot reach into the worker's pool.
        "APIPI_SANDBOX_TTL_OPENAI_HOSTED": "2s",
    }
    async with split_http_client(
        store,
        tmp_path,
        env=env,
        worker_env={"APIPI_SESSIONS_DIR": str(microvm_settings.sessions_dir)},
        timeout=90,
        http_timeout=60,
    ) as client:
        yield client


async def test_microvm_openai_hosted_streams_fake_pi_text(
    microvm_client: AsyncClient, microvm_settings: Settings
) -> None:
    token = "e2e-microvm"
    created_agent = await microvm_client.post(
        "/v1/agents",
        headers=auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await microvm_client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    from tests.support.workspace import hosted_dir

    directory = hosted_dir(microvm_settings, token, body["id"])
    assert directory.is_dir()
    root = Path(microvm_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    session_id = body["id"]
    turned = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "hello-microvm"},
    )
    assert turned.status_code == 200
    events = await microvm_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-microvm"


@pytest.mark.slow
def test_worker_probe_boots_throwaway_guest(microvm_settings: Settings) -> None:
    probe_run_mode(microvm_settings)


@pytest.mark.slow
async def test_microvm_workspace_persists_after_guest_stop(
    microvm_client: AsyncClient, microvm_settings: Settings
) -> None:
    token = "e2e-microvm-persist"
    created_agent = await microvm_client.post(
        "/v1/agents",
        headers=auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await microvm_client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": created_agent.json()["id"]},
    )
    assert created.status_code == 200
    from tests.support.workspace import hosted_dir

    directory = hosted_dir(microvm_settings, token, created.json()["id"])
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    session_id = uuid.UUID(created.json()["id"])
    turned = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "persist-me"},
    )
    assert turned.status_code == 200
    await asyncio.sleep(6)  # worker reaps the guest after the 2s TTL
    assert not (directory / "keep.txt").exists()
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    again = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert again.status_code == 200
