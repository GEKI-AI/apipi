from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.worker.pi.isolation.none import NoneIsolation
from apipi.worker.pi.proc import PiProc


class ExampleIsolation:
    name = "example"
    needs_probe = False
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
    ) -> PiProc:
        return await self._inner.spawn(
            settings,
            cwd=cwd,
            tools=tools,
            mcp_http=mcp_http,
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
        )
