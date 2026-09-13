from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.microvm import probe_microvm, require_microvm, spawn_microvm_pi
from apipi.pi.proc import PiProc


class MicrovmIsolation:
    name = "microvm"
    needs_probe = True
    stdio_on_host = False
    warn_not_production = False

    def require(self, settings: Settings | None) -> None:
        require_microvm(settings)

    async def probe(self, settings: Settings) -> None:
        await probe_microvm(settings)

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
        return await spawn_microvm_pi(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        )
