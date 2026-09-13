from typing import Protocol

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.proc import PiProc


class Isolation(Protocol):
    name: str
    needs_probe: bool
    stdio_on_host: bool
    warn_not_production: bool

    def require(self, settings: Settings | None) -> None: ...

    async def probe(self, settings: Settings) -> None: ...

    async def spawn(
        self,
        settings: Settings,
        *,
        cwd: str | None,
        tools: bool,
        mcp_http: list[McpHttpServer] | None = None,
        mcp_stdio: list[McpStdioServer] | None = None,
        skill_dirs: list[str] | None = None,
    ) -> PiProc: ...
