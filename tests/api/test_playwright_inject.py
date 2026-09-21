from pathlib import Path

from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store
from apipi.worker.pi.platform_prompt import BROWSER_HINT
from apipi.worker.pi.sandbox import PLAYWRIGHT_LABEL


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _microvm_settings(
    settings: Settings,
    tmp_path: Path,
    *,
    sandbox_auto_playwright: bool = True,
) -> Settings:
    kernel = tmp_path / "vmlinux"
    rootfs = tmp_path / "rootfs.ext4"
    browser = tmp_path / "rootfs-browser.ext4"
    kernel.write_bytes(b"k")
    rootfs.write_bytes(b"r")
    browser.write_bytes(b"b")
    return Settings(
        database_url=settings.database_url,
        run_mode="microvm",
        sessions_dir=settings.sessions_dir,
        microvm_kernel=str(kernel),
        microvm_rootfs=str(rootfs),
        microvm_rootfs_browser=str(browser),
        sandbox_auto_playwright=sandbox_auto_playwright,
    )


async def test_l_injects_playwright(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    harness = FakeHarness()
    app = create_app(
        _microvm_settings(settings, tmp_path), store=store, harness=harness
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "l-playwright"
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test"},
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none", "sandbox_size": "L"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
    assert harness.mcp_stdio is not None
    assert [server.server_label for server in harness.mcp_stdio] == [PLAYWRIGHT_LABEL]
    assert harness.instructions is not None
    assert BROWSER_HINT in harness.instructions


async def test_l_skips_inject_when_auto_off(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    harness = FakeHarness()
    app = create_app(
        _microvm_settings(settings, tmp_path, sandbox_auto_playwright=False),
        store=store,
        harness=harness,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "l-no-auto"
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test"},
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none", "sandbox_size": "L"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
    assert harness.mcp_stdio == []
    assert harness.instructions is not None
    assert BROWSER_HINT not in harness.instructions


async def test_l_does_not_duplicate_caller_playwright(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    harness = FakeHarness()
    app = create_app(
        _microvm_settings(settings, tmp_path), store=store, harness=harness
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "l-dup"
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={
                "name": "bot",
                "model": "test",
                "tools": [
                    {
                        "type": "mcp",
                        "server_label": "playwright",
                        "transport": {
                            "type": "stdio",
                            "command": "npx",
                            "args": ["-y", "@playwright/mcp@1.0.0"],
                        },
                    }
                ],
            },
        )
        created = await client.post(
            "/v1/agents/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none", "sandbox_size": "L"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
    assert harness.mcp_stdio is not None
    assert [server.server_label for server in harness.mcp_stdio] == ["playwright"]
    assert harness.mcp_stdio[0].args == ["-y", "@playwright/mcp@1.0.0"]
    assert harness.instructions is not None
    assert BROWSER_HINT in harness.instructions


async def test_chat_does_not_inject_playwright(
    settings: Settings, store: Store, tmp_path: Path
) -> None:
    harness = FakeHarness()
    app = create_app(
        _microvm_settings(settings, tmp_path), store=store, harness=harness
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        token = "chat-no-pw"
        agent = await client.post(
            "/v1/agents",
            headers=_auth(token),
            json={"name": "bot", "model": "test"},
        )
        created = await client.post(
            "/v1/chat/sessions",
            headers=_auth(token),
            json={
                "agent_id": agent.json()["id"],
                "metadata": {"apipi.sandbox_size": "L"},
                "input": "hello",
            },
        )
        assert created.status_code == 200
    assert harness.mcp_stdio == []
    assert harness.instructions is not None
    assert BROWSER_HINT not in harness.instructions
