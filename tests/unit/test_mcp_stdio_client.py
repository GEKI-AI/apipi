import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_mcp_client_speaks_newline_json() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is required to test the stdio MCP client")
    runner = ROOT / "tests" / "support" / "mcp_stdio_client_check.mjs"
    fixture = ROOT / "tests" / "support" / "mcp_stdio_fixture.py"
    proc = subprocess.run(
        [node, str(runner), sys.executable, str(fixture)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
