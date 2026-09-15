import uuid
from collections.abc import AsyncIterator
from typing import Any

from apipi.env.computer import Computer
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.pi.map import map_pi_event
from apipi.pi.pool import PiPool


class PiHarness:
    def __init__(self, pool: PiPool) -> None:
        self.pool = pool

    async def generate(
        self,
        text: str,
        *,
        session_id: uuid.UUID | None = None,
        cwd: str | None = None,
        tools: bool = True,
        mcp_http: list[McpHttpServer] | None = None,
        mcp_stdio: list[McpStdioServer] | None = None,
        skill_dirs: list[str] | None = None,
        computer: Computer | None = None,
        tenant_id: uuid.UUID | None = None,
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if session_id is None:
            return
        model = _kwargs.get("model")
        api_key = _kwargs.get("api_key")
        key_id = _kwargs.get("key_id")
        raw_env_type = _kwargs.get("env_type")
        env_type = raw_env_type if isinstance(raw_env_type, str) else None
        raw_instructions = _kwargs.get("instructions")
        instructions = (
            raw_instructions
            if isinstance(raw_instructions, str) and raw_instructions
            else None
        )
        proc = await self.pool.get(
            session_id,
            cwd=cwd,
            tools=False if computer is not None else tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
            tenant_id=tenant_id,
            model=model if isinstance(model, str) else None,
            instructions=instructions,
            api_key=api_key if isinstance(api_key, str) else None,
            key_id=key_id if isinstance(key_id, str) else None,
            env_type=env_type,
        )
        settled = False
        async for event in proc.prompt(text):
            if event.get("type") == "agent_settled":
                settled = True
            for public in map_pi_event(event):
                yield public
        if not settled:
            yield (
                "pi_error",
                {"message": "Pi stopped before the turn finished"},
            )
        self.pool.touch(session_id)

    async def abort(self, session_id: uuid.UUID) -> None:
        proc = self.pool.peek(session_id)
        if proc is None:
            return
        await proc.abort()
