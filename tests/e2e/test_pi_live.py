import asyncio
import shutil

import pytest

from apipi.worker.pi.proc import PiProc
from apipi.worker.pi.version import PINNED_PI

pytestmark = pytest.mark.slow


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi not installed")
def test_real_pi_is_local_only() -> None:
    assert shutil.which("pi") is not None


@pytest.mark.skipif(shutil.which("pi") is None, reason="pi not installed")
def test_real_pi_slash_command_ends_turn() -> None:
    async def _run() -> list[dict[str, object]]:
        proc = await asyncio.create_subprocess_exec(
            "pi",
            "--mode",
            "rpc",
            "--no-session",
            "--no-extensions",
            "-e",
            "builtin:mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pi = PiProc(proc)
        try:
            async with asyncio.timeout(30):
                return [event async for event in pi.prompt("/mcp")]
        finally:
            await pi.terminate()

    events = asyncio.run(_run())
    assert events[-1].get("type") == "agent_settled"
    end = next(item for item in events if item.get("type") == "agent_end")
    messages = end.get("messages")
    assert isinstance(messages, list)
    first = messages[0]
    assert isinstance(first, dict)
    assert str(first.get("errorMessage")).startswith("input_handled_by_command")
    assert PINNED_PI == "0.99.1"
