from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.isolation.none import NoneIsolation
from apipi.pi.proc import PiProc


class ExampleIsolation:
    name = "example"
    needs_probe = False
    stdio_on_host = True
    warn_not_production = True

    def __init__(self) -> None:
        self._inner = NoneIsolation()

    def require(self, settings: Settings | None) -> None:
        self._inner.require(settings)

    async def probe(self, settings: Settings) -> None:
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
        model: str | None = None,
        api_key: str | None = None,
    ) -> PiProc:
        return await self._inner.spawn(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
            model=model,
            api_key=api_key,
        )
