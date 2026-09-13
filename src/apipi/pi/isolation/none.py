import asyncio

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.proc import PiProc, pi_command_args, pi_env


class NoneIsolation:
    name = "none"
    needs_probe = False
    stdio_on_host = True
    warn_not_production = True

    def require(self, settings: Settings | None) -> None:
        del settings

    async def probe(self, settings: Settings) -> None:
        del settings

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
        args = pi_command_args(
            settings,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
        )
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=pi_env(settings, mcp_http, mcp_stdio),
        )
        return PiProc(process)
