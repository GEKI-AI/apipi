import uuid
from collections.abc import AsyncIterator
from typing import Any

from apipi.mcp.http import McpHttpServer
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
        **_kwargs: object,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if session_id is None:
            return
        proc = await self.pool.get(session_id, cwd=cwd, tools=tools, mcp_http=mcp_http)
        async for event in proc.prompt(text):
            for public in map_pi_event(event):
                yield public
        self.pool.touch(session_id)
