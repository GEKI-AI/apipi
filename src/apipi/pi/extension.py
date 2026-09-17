from pathlib import Path

from apipi.config import Settings
from apipi.pi.dirs import sessions_root

MCP_EXTENSION_REL = ".pi/agent/extensions/apipi-mcp.ts"
GUEST_MCP_EXTENSION = "/workspace/.pi/agent/extensions/apipi-mcp.ts"


def mcp_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp.ts").read_bytes()


def install_mcp_extension(dest_dir: Path) -> Path:
    path = dest_dir / "apipi-mcp.ts"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(mcp_extension_source())
    return path


def host_mcp_extension(settings: Settings, cwd: str | None) -> str:
    if cwd:
        dest = Path(cwd) / ".pi" / "agent" / "extensions"
    else:
        dest = sessions_root(settings) / ".pi" / "agent" / "extensions"
    return str(install_mcp_extension(dest))
