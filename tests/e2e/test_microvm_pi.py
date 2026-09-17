import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from apipi.config import ConfigError, Settings
from apipi.gateway import create_app
from apipi.store.engine import Store
from apipi.worker.pi.microvm import microvm_images, require_microvm
from apipi.worker.pi.probe import probe_run_mode

pytestmark = [pytest.mark.e2e, pytest.mark.microvm]

_FAKE_PI = Path(__file__).resolve().parents[1] / "support" / "fake_pi.py"
_GUEST_FAKE_PI = "/workspace/fake_pi.py"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _image_paths() -> tuple[str | None, str | None]:
    try:
        return microvm_images()
    except ConfigError:
        return None, None


def _tap_or_skip() -> None:
    ip_bin = shutil.which("ip")
    if ip_bin is None:
        pytest.skip("APIPI_RUN_MODE=microvm requires ip")
    name = f"apipit{os.getpid()}"
    added = subprocess.run(
        [ip_bin, "tuntap", "add", "dev", name, "mode", "tap"],
        capture_output=True,
    )
    subprocess.run([ip_bin, "link", "delete", "dev", name], capture_output=True)
    if added.returncode != 0:
        pytest.skip("APIPI_RUN_MODE=microvm requires TAP")


def _microvm_or_skip(settings: Settings) -> None:
    try:
        require_microvm(settings)
    except ConfigError as exc:
        pytest.skip(str(exc))
    _tap_or_skip()


@pytest.fixture
def microvm_settings(tmp_path: Path) -> Settings:
    kernel, rootfs = _image_paths()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        idle_ttl=timedelta(seconds=30),
        pi_command=f"python3 {_GUEST_FAKE_PI}",
        sessions_dir=str(tmp_path / "sessions"),
        microvm_kernel=kernel,
        microvm_rootfs=rootfs,
    )
    _microvm_or_skip(settings)
    return settings


@pytest.fixture
def microvm_app(microvm_settings: Settings, store: Store) -> FastAPI:
    return create_app(microvm_settings, store=store)


@pytest.fixture
async def microvm_client(microvm_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=microvm_app),
        base_url="http://test",
        timeout=60,
    ) as client:
        yield client


async def test_microvm_openai_hosted_streams_fake_pi_text(
    microvm_client: AsyncClient, microvm_settings: Settings
) -> None:
    token = "e2e-microvm"
    created_agent = await microvm_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    agent_id = created_agent.json()["id"]
    created = await microvm_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": agent_id},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["environment"]["type"] == "openai_hosted"
    directory = Path(body["environment"]["directory"])
    assert directory.is_dir()
    root = Path(microvm_settings.sessions_dir or ".")
    assert directory.is_relative_to(root)
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    session_id = body["id"]
    turned = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "hello-microvm"},
    )
    assert turned.status_code == 200
    events = await microvm_client.get(
        f"/v1/agents/sessions/{session_id}/events", headers=_auth(token)
    )
    done = [
        event
        for event in events.json()["data"]
        if event["type"] == "agent.session.turn.output_text.done"
    ]
    assert done[0]["data"]["text"] == "hello-microvm"


@pytest.mark.slow
def test_prepare_serve_boots_throwaway_guest(microvm_settings: Settings) -> None:
    probe_run_mode(microvm_settings)


@pytest.mark.slow
async def test_microvm_workspace_persists_after_guest_stop(
    microvm_client: AsyncClient, microvm_app: FastAPI, microvm_settings: Settings
) -> None:
    token = "e2e-microvm-persist"
    created_agent = await microvm_client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    created = await microvm_client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={"agent_id": created_agent.json()["id"]},
    )
    assert created.status_code == 200
    directory = Path(created.json()["environment"]["directory"])
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    session_id = uuid.UUID(created.json()["id"])
    turned = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "persist-me"},
    )
    assert turned.status_code == 200
    await microvm_app.state.pi_pool.kill(session_id)
    assert not (directory / "keep.txt").exists()
    shutil.copy(_FAKE_PI, directory / "fake_pi.py")
    again = await microvm_client.post(
        f"/v1/agents/sessions/{session_id}/events",
        headers=_auth(token),
        json={"type": "agent.session.input.message", "content": "again"},
    )
    assert again.status_code == 200
