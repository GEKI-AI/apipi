from typing import Any

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.worker.pi.isolation.none import NoneIsolation
from apipi.worker.pi.proc import PiProc


class FakeIsolation:
    name = "fake"
    needs_probe = True
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
    ) -> PiProc:
        type(self).spawned = True
        return await self._inner.spawn(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
            function_tools=function_tools,
            skill_dirs=skill_dirs,
            model=model,
            instructions=instructions,
            api_key=api_key,
            mem_mib=mem_mib,
            image=image,
            extra_env=extra_env,
            thinking=thinking,
            system_prompt=system_prompt,
            system_prompt_set=system_prompt_set,
            codemode=codemode,
            env_type=env_type,
            session_id=session_id,
        )


class IncompleteIsolation:
    name = "incomplete"
