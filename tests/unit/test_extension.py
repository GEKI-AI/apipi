from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.extension import (
    GUEST_MCP_EXTENSION,
    MCP_EXTENSION_REL,
    host_mcp_extension,
    mcp_extension_source,
)


def test_mcp_extension_source_is_pi_module() -> None:
    text = mcp_extension_source().decode()
    assert "attachStdio" in text
    assert "APIPI_MCP_STDIO" in text
    assert "Do not install Playwright" in text
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
