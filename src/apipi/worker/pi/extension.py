from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.dirs import sessions_root

MCP_EXTENSION_REL = ".pi/agent/extensions/apipi-mcp.ts"
MCP_CLIENT_REL = ".pi/agent/extensions/mcp_client.mjs"
MCP_HTTP_REL = ".pi/agent/extensions/mcp_http.mjs"
GUEST_MCP_EXTENSION = "/workspace/.pi/agent/extensions/apipi-mcp.ts"


def mcp_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp.ts").read_bytes()


def mcp_client_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp_client.mjs").read_bytes()


def mcp_http_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp_http.mjs").read_bytes()


def install_mcp_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / "mcp_client.mjs").write_bytes(mcp_client_source())
    (dest_dir / "mcp_http.mjs").write_bytes(mcp_http_source())
    path = dest_dir / "apipi-mcp.ts"
    path.write_bytes(mcp_extension_source())
    return path


def host_mcp_extension(settings: Settings, cwd: str | None) -> str:
    if cwd:
        dest = Path(cwd) / ".pi" / "agent" / "extensions"
    else:
        dest = sessions_root(settings) / ".pi" / "agent" / "extensions"
    return str(install_mcp_extension(dest))
