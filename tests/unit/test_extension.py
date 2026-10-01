from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.extension import (
    APIPI_EXTENSION_REL,
    GUEST_APIPI_EXTENSION,
    GUEST_MCP_EXTENSION,
    MCP_EXTENSION_REL,
    apipi_extension_source,
    host_mcp_extension,
    mcp_extension_source,
)
from apipi.worker.pi.version import PINNED_AGENT_BROWSER


def test_mcp_extension_source_is_pi_module() -> None:
    text = mcp_extension_source().decode()
    assert "registerMcpServer" in text
    assert "APIPI_MCP_SERVERS" in text
    assert "toolExposure" in text
    assert "APIPI_IMAGE_CHECK" in text
    assert "attachStdio" not in text
    assert "APIPI_MCP_STDIO" not in text
    assert "mcp_client.mjs" not in text
    assert "mcp_http.mjs" not in text
    assert "tools/call" not in text
    assert MCP_EXTENSION_REL.endswith("apipi-mcp.ts")
    assert GUEST_MCP_EXTENSION.endswith("apipi-mcp.ts")


def test_apipi_extension_holds_identity() -> None:
    text = apipi_extension_source().decode()
    assert "before_agent_start" in text
    assert "identity.txt" in text
    assert "DEFAULT_BASH_TIMEOUT_SEC" in text
    assert "registerMcpServer" not in text
    assert APIPI_EXTENSION_REL.endswith("apipi.ts")
    assert GUEST_APIPI_EXTENSION.endswith("apipi.ts")


def test_host_mcp_extension_writes_file(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    cwd = tmp_path / "session"
    cwd.mkdir()
    paths = [Path(item) for item in host_mcp_extension(settings, str(cwd))]
    assert [item.name for item in paths] == ["apipi.ts", "apipi-mcp.ts"]
    assert all(item.is_file() for item in paths)
    assert "registerMcpServer" in paths[1].read_text()
    assert "before_agent_start" in paths[0].read_text()


def test_browser_recipe_pins_agent_browser() -> None:
    root = Path(__file__).resolve().parents[2]
    setup = (root / "images" / "browser" / "setup.sh").read_text()
    assert "agent-browser" in setup
    assert "@playwright/mcp" not in setup
    assert "/opt/chrome-headless-shell" in setup
    assert "AGENT_BROWSER_NO_WEBMCP=1" in setup
    assert PINNED_AGENT_BROWSER == "0.38.1"
    guest = (root / "src" / "apipi" / "worker" / "pi" / "guest.sh").read_text()
    assert "npm_config_cache=/tmp/npm-cache" in guest
    assert "UV_CACHE_DIR=/tmp/uv-cache" in guest
    assert "cp -a /var/cache/npm/." in guest
    assert 'size="$SHM_SIZE"' in guest
    assert "/etc/apipi/browser.env" in guest
    assert 'PATH="$WS/.venv/bin:$PATH"' in guest
    assert 'PATH="$WS/.npm/bin:$PATH"' in guest
    assert "mount -t devpts devpts /dev/pts" in guest
    image = (root / "images" / "browser" / "image.env").read_text()
    assert "fonts-noto-cjk" in image
    assert "fonts-noto-color-emoji" in image
    assert "ARCHS=x86_64" in image
    assert "MIN_VCPUS=2" in image
