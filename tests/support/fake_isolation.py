from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.isolation.none import NoneIsolation
from apipi.pi.proc import PiProc


class FakeIsolation:
    name = "fake"
    needs_probe = True
    stdio_on_host = True
    warn_not_production = True
    required = False
    probed = False
    spawned = False

    def __init__(self) -> None:
        self._inner = NoneIsolation()

    def require(self, settings: Settings | None) -> None:
        type(self).required = True
        self._inner.require(settings)

    async def probe(self, settings: Settings) -> None:
        type(self).probed = True
        await self._inner.probe(settings)

    async def spawn(
        self,
        settings: Settings,
        *,
        cwd: str | None,
        tools: bool,
        mcp_http: list[McpHttpServer] | None = None,
        mcp_stdio: list[McpStdioServer] | None = None,
        skill_dirs: list[str] | None = None,
    ) -> PiProc:
        type(self).spawned = True
        return await self._inner.spawn(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        )


class IncompleteIsolation:
    name = "incomplete"
