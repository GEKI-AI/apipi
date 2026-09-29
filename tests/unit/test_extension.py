from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.extension import (
    GUEST_MCP_EXTENSION,
    MCP_EXTENSION_REL,
    host_mcp_extension,
    mcp_extension_source,
)
from apipi.worker.pi.sandbox import PLAYWRIGHT_MCP_CLI


def test_mcp_extension_source_is_pi_module() -> None:
    text = mcp_extension_source().decode()
    assert "attachStdio" in text
    assert "APIPI_MCP_STDIO" in text
    assert "mcp_client.mjs" in text
    assert "mcp_http.mjs" in text
    assert "APIPI_MCP_SERVERS" in text
    assert "bash-install.txt" in text
    assert "tools/call" in text
    assert "failed after" in text
    assert "Do not install Playwright" not in text
    assert "Save screenshots" not in text
    assert "Today is" not in text
    assert "playwrightTools" in text
    assert "waitForSpawn" not in text
    assert "Content-Length" not in text
    assert "ATTACH_TIMEOUT_MS = 15_000" in text
    assert "npm_config_cache" in text
    assert MCP_EXTENSION_REL.endswith("apipi-mcp.ts")
    assert GUEST_MCP_EXTENSION.endswith("apipi-mcp.ts")


def test_host_mcp_extension_writes_file(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    cwd = tmp_path / "session"
    cwd.mkdir()
    path = Path(host_mcp_extension(settings, str(cwd)))
    assert path.is_file()
    assert path.name == "apipi-mcp.ts"
    assert "session_start" in path.read_text()
    assert (path.parent / "mcp_client.mjs").is_file()
    assert (path.parent / "mcp_http.mjs").is_file()


def test_browser_recipe_vendors_playwright_mcp() -> None:
    root = Path(__file__).resolve().parents[2]
    setup = (root / "images" / "browser" / "setup.sh").read_text()
    assert "@playwright/mcp@" in setup
    assert "@latest" not in setup
    assert "/opt/apipi/playwright-mcp" in setup
    assert "node_modules/@playwright/mcp/cli.js" in setup
    assert PLAYWRIGHT_MCP_CLI.endswith("node_modules/@playwright/mcp/cli.js")
    guest = (root / "src" / "apipi" / "worker" / "pi" / "guest.sh").read_text()
    assert "npm_config_cache=/tmp/npm-cache" in guest
    assert "UV_CACHE_DIR=/tmp/uv-cache" in guest
    assert "cp -a /var/cache/npm/." in guest
    assert "mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm" in guest
    assert 'PATH="$WS/.venv/bin:$PATH"' in guest
    assert 'PATH="$WS/.npm/bin:$PATH"' in guest
    assert "mount -t devpts devpts /dev/pts" in guest
    image = (root / "images" / "browser" / "image.env").read_text()
    assert "font-noto-cjk" in image
    assert "font-noto-emoji" in image
