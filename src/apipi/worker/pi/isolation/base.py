from typing import Any, Protocol

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.worker.pi.proc import PiProc


class Isolation(Protocol):
    name: str
    needs_probe: bool
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
        function_tools: list[dict[str, Any]] | None = None,
        skill_dirs: list[str] | None = None,
        model: str | None = None,
        instructions: str | None = None,
        api_key: str | None = None,
        mem_mib: int | None = None,
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
        thinking: str | None = None,
        system_prompt: str | None = None,
        system_prompt_set: bool = False,
        codemode: str = "off",
        env_type: str | None = None,
        session_id: str | None = None,
        web_search: bool = False,
    ) -> PiProc: ...
