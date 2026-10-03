from pathlib import Path

from apipi.common.dirs import sessions_root
from apipi.config import Settings

APIPI_EXTENSION_REL = ".pi/agent/extensions/apipi.ts"
MCP_EXTENSION_REL = ".pi/agent/extensions/apipi-mcp.ts"
WEB_SEARCH_EXTENSION_REL = ".pi/agent/extensions/apipi-web-search.ts"
GUEST_APIPI_EXTENSION = "/workspace/.pi/agent/extensions/apipi.ts"
GUEST_MCP_EXTENSION = "/workspace/.pi/agent/extensions/apipi-mcp.ts"
GUEST_WEB_SEARCH_EXTENSION = "/workspace/.pi/agent/extensions/apipi-web-search.ts"


def apipi_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "apipi.ts").read_bytes()


def mcp_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "mcp.ts").read_bytes()


def web_search_extension_source() -> bytes:
    return (Path(__file__).with_name("extensions") / "web_search.ts").read_bytes()


def guest_extensions(web_search: bool) -> list[str]:
    paths = [GUEST_APIPI_EXTENSION, GUEST_MCP_EXTENSION]
    if web_search:
        paths.append(GUEST_WEB_SEARCH_EXTENSION)
    return paths


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


def install_web_search_extension(dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "apipi-web-search.ts"
    path.write_bytes(web_search_extension_source())
    return path


def host_mcp_extension(
    settings: Settings, cwd: str | None, *, web_search: bool = False
) -> list[str]:
    if cwd:
        dest = Path(cwd) / ".pi" / "agent" / "extensions"
    else:
        dest = sessions_root(settings) / ".pi" / "agent" / "extensions"
    install_mcp_extension(dest)
    paths = [str(dest / "apipi.ts"), str(dest / "apipi-mcp.ts")]
    if web_search:
        paths.append(str(install_web_search_extension(dest)))
    return paths
