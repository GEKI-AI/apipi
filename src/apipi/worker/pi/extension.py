from pathlib import Path

from apipi.config import Settings
from apipi.worker.pi.dirs import sessions_root

APIPI_EXTENSION_REL = ".pi/agent/extensions/apipi.ts"
MCP_EXTENSION_REL = ".pi/agent/extensions/apipi-mcp.ts"
GUEST_APIPI_EXTENSION = "/workspace/.pi/agent/extensions/apipi.ts"
GUEST_MCP_EXTENSION = "/workspace/.pi/agent/extensions/apipi-mcp.ts"


def apipi_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "apipi.ts").read_bytes()


def mcp_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp.ts").read_bytes()


def install_apipi_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "apipi.ts"
    path.write_bytes(apipi_extension_source())
    return path


def install_mcp_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    install_apipi_extension(dest_dir)
    path = dest_dir / "apipi-mcp.ts"
    path.write_bytes(mcp_extension_source())
    return path


def host_mcp_extension(settings: Settings, cwd: str | None) -> list[str]:
    if cwd:
        dest = Path(cwd) / ".pi" / "agent" / "extensions"
    else:
        dest = sessions_root(settings) / ".pi" / "agent" / "extensions"
    install_mcp_extension(dest)
    return [str(dest / "apipi.ts"), str(dest / "apipi-mcp.ts")]
