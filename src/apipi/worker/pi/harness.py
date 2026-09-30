import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from apipi.env.computer import Computer
from apipi.mcp.http import McpHttpServer
from apipi.services.failures import failure_for, pi_payload
from apipi.worker.pi.map import ThinkingTracker, map_pi_event
from apipi.worker.pi.pool import PiPool


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
        raw_mem = _kwargs.get("mem_mib")
        mem_mib = raw_mem if isinstance(raw_mem, int) else None
        raw_image = _kwargs.get("image")
        image = raw_image if isinstance(raw_image, str) else None
        raw_extra = _kwargs.get("extra_env")
        extra_env = (
            {str(key): str(value) for key, value in raw_extra.items()}
            if isinstance(raw_extra, dict)
            else None
        )
        raw_instructions = _kwargs.get("instructions")
        instructions = (
            raw_instructions
            if isinstance(raw_instructions, str) and raw_instructions
            else None
        )
        raw_thinking = _kwargs.get("thinking")
        thinking = raw_thinking if isinstance(raw_thinking, str) else None
        raw_prompt = _kwargs.get("system_prompt")
        system_prompt = raw_prompt if isinstance(raw_prompt, str) else None
        system_prompt_set = _kwargs.get("system_prompt_set") is True
        raw_idle = _kwargs.get("idle_ttl")
        idle_ttl = raw_idle if isinstance(raw_idle, timedelta) else None
        idle_ttl_set = _kwargs.get("idle_ttl_set") is True
        raw_agent = _kwargs.get("agent_id")
        raw_user = _kwargs.get("user_id")
        raw_org = _kwargs.get("org_id")
        raw_codemode = _kwargs.get("codemode")
        codemode = raw_codemode if isinstance(raw_codemode, str) else "off"
        raw_turn = _kwargs.get("turn_id")
        turn_id = str(raw_turn) if raw_turn else None
        raw_revision = _kwargs.get("agent_revision")
        agent_revision = raw_revision if isinstance(raw_revision, int) else None
        agent_id = str(raw_agent) if raw_agent else None
        proc = await self.pool.get(
            session_id,
            cwd=cwd,
            tools=False if computer is not None else tools,
            mcp_http=mcp_http,
            skill_dirs=skill_dirs,
            tenant_id=tenant_id,
            model=model if isinstance(model, str) else None,
            instructions=instructions,
            api_key=api_key if isinstance(api_key, str) else None,
            key_id=key_id if isinstance(key_id, str) else None,
            env_type=env_type,
            mem_mib=mem_mib,
            image=image,
            extra_env=extra_env,
            thinking=thinking,
            system_prompt=system_prompt,
            system_prompt_set=system_prompt_set,
            codemode=codemode,
            idle_ttl=idle_ttl,
            idle_ttl_set=idle_ttl_set,
            agent_id=agent_id,
            user_id=raw_user if isinstance(raw_user, str) else None,
            org_id=raw_org if isinstance(raw_org, str) else None,
        )
        broker = getattr(proc, "broker", None)
        if broker is not None:
            set_context = getattr(broker, "set_context", None)
            if callable(set_context):
                set_context(str(session_id), agent_id)
            set_turn = getattr(broker, "set_turn", None)
            if callable(set_turn):
                set_turn(turn_id, agent_revision)
        settled = False
        thinking = ThinkingTracker()
        abort = _kwargs.get("abort")
        raw_images = _kwargs.get("images")
        images = raw_images if isinstance(raw_images, list) and raw_images else None
        for server in mcp_http or []:
            tool_list = [
                {"name": tool.name, "description": tool.description}
                for tool in getattr(server, "tools", ())
            ]
            yield (
                "agent.session.turn.item.added",
                {
                    "item_type": "mcp_list_tools",
                    "server_label": server.server_label,
                    "tools": tool_list,
                },
            )
        stream = proc.prompt(text, images=images) if images else proc.prompt(text)
        try:
            async for event in stream:
                if event.get("type") == "agent_settled":
                    settled = True
                for public in map_pi_event(event):
                    yield public
                for public in thinking.feed(event):
                    yield public
        finally:
            if broker is not None:
                clear_turn = getattr(broker, "clear_turn", None)
                if callable(clear_turn):
                    clear_turn()
        if not settled:
            if getattr(abort, "is_set", lambda: False)():
                self.pool.touch(session_id)
                return
            code = "pi_memory" if proc.stop_reason == "memory" else "pi_exited"
            yield (
                "pi_error",
                pi_payload(failure_for(code, "Pi stopped before the turn finished")),
            )
        self.pool.touch(session_id)

    async def abort(self, session_id: uuid.UUID) -> None:
        proc = self.pool.peek(session_id)
        if proc is None:
            return
        await proc.abort()
