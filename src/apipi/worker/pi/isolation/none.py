import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from apipi.config import Settings
from apipi.mcp.http import McpHttpServer
from apipi.mcp.stdio import McpStdioServer
from apipi.worker.pi.dirs import PI_SESSION_REL, pi_session_file
from apipi.worker.pi.extension import host_mcp_extension
from apipi.worker.pi.proc import PiProc, pi_command_args, pi_env

log = logging.getLogger("apipi.worker.pi")


async def _log_stderr(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode(errors="replace").rstrip()
        if text:
            log.warning("pi stderr", extra={"line": text[:500]})


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
        model: str | None = None,
        instructions: str | None = None,
        api_key: str | None = None,
        mem_mib: int | None = None,
        image: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> PiProc:
        del mem_mib, image
        session_file = None
        scratch: str | None = None
        if cwd:
            root = Path(cwd)
            root.mkdir(parents=True, exist_ok=True)
            path = pi_session_file(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            session_file = PI_SESSION_REL
            agent_root = root
        else:
            scratch = tempfile.mkdtemp(prefix="apipi-pi-")
            agent_root = Path(scratch)
        from apipi.worker.pi.broker import start_broker
        from apipi.worker.pi.model_host import models_json_for_base_url

        args = pi_command_args(
            settings,
            tools=tools,
            mcp_http=mcp_http,
            mcp_stdio=mcp_stdio,
            skill_dirs=skill_dirs,
            model=model,
            instructions=instructions,
            session_file=session_file,
            extension=host_mcp_extension(settings, cwd),
        )
        broker = await start_broker(
            settings,
            api_key=api_key,
            mcp_http=mcp_http,
            host="127.0.0.1",
            port=0,
        )
        try:
            env = pi_env(
                settings,
                mcp_http,
                mcp_stdio,
                api_key=api_key,
                broker=broker,
                extra_env=extra_env,
            )
            agent_dir = agent_root / ".pi" / "agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "models.json").write_bytes(
                models_json_for_base_url(settings, broker.openai_base_url)
            )
            env["PI_CODING_AGENT_DIR"] = str(agent_dir)
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except BaseException:
            await broker.stop()
            if scratch is not None:
                shutil.rmtree(scratch, ignore_errors=True)
            raise
        stderr_task = asyncio.create_task(_log_stderr(process.stderr))
        return PiProc(
            process,
            broker=broker,
            process_group=True,
            scratch_dir=scratch,
            stderr_task=stderr_task,
        )
