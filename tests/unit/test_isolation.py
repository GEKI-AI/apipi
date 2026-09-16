from typing import Any

import pytest
from tests.support.fake_isolation import FakeIsolation

from apipi.config import ConfigError, Settings, require_run_mode
from apipi.mcp.stdio import McpStdioServer, start_mcp_stdio_tools
from apipi.pi.isolation import load_isolation
from apipi.pi.isolation.microvm import MicrovmIsolation
from apipi.pi.isolation.none import NoneIsolation
from apipi.pi.proc import spawn_pi


def _settings(run_mode: str = "none") -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode=run_mode,
        pi_command="pi",
    )


def test_none_isolation_contract() -> None:
    backend = load_isolation("none")
    assert isinstance(backend, NoneIsolation)
    assert backend.name == "none"
    assert backend.needs_probe is False
    assert backend.stdio_on_host is True
    assert backend.warn_not_production is True
    backend.require(_settings())


def test_microvm_isolation_contract() -> None:
    backend = load_isolation("microvm")
    assert isinstance(backend, MicrovmIsolation)
    assert backend.name == "microvm"
    assert backend.needs_probe is True
    assert backend.stdio_on_host is False
    assert backend.warn_not_production is False


def test_host_and_jail_are_not_valid() -> None:
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=host is not valid"):
        load_isolation("host")
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=jail is not valid"):
        load_isolation("jail")
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=host is not valid"):
        require_run_mode("host")
    with pytest.raises(ConfigError, match="APIPI_RUN_MODE=jail is not valid"):
        require_run_mode("jail")


def test_unknown_mode_without_import_path() -> None:
    with pytest.raises(ConfigError, match=r"none, microvm, or package\.mod:Class"):
        load_isolation("gvisor")


def test_custom_backend_missing() -> None:
    with pytest.raises(ConfigError, match="backend not found"):
        load_isolation("tests.support.missing_isolation:Nope")


def test_custom_backend_incomplete() -> None:
    with pytest.raises(ConfigError, match="backend missing"):
        load_isolation("tests.support.fake_isolation:IncompleteIsolation")


def test_custom_backend_loads_and_probes() -> None:
    FakeIsolation.required = False
    FakeIsolation.probed = False
    path = "tests.support.fake_isolation:FakeIsolation"
    backend = load_isolation(path)
    assert backend.name == "fake"
    assert backend.needs_probe is True
    backend.require(_settings(path))
    assert FakeIsolation.required is True


async def test_custom_backend_probe_and_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeIsolation.probed = False
    FakeIsolation.spawned = False
    path = "tests.support.fake_isolation:FakeIsolation"
    backend = load_isolation(path)

    async def fake_exec(*_args: object, **_kwargs: object) -> Any:
        class Process:
            returncode = None
            stdin = None
            stdout = None
            stderr = None
            pid = 1

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = 0

            async def wait(self) -> int:
                return 0

        return Process()

    monkeypatch.setattr(
        "apipi.pi.isolation.none.asyncio.create_subprocess_exec", fake_exec
    )
    await backend.probe(_settings(path))
    assert FakeIsolation.probed is True
    proc = await backend.spawn(_settings(path), cwd=None, tools=False)
    assert FakeIsolation.spawned is True
    assert proc.alive
    await proc.terminate()
    assert not proc.alive


async def test_spawn_pi_none_starts_child(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    async def fake_exec(*args: object, **kwargs: object) -> Any:
        created["args"] = args
        created["cwd"] = kwargs.get("cwd")

        class Process:
            returncode = None
            stdin = None
            stdout = None
            stderr = None

        return Process()

    monkeypatch.setattr(
        "apipi.pi.isolation.none.asyncio.create_subprocess_exec", fake_exec
    )
    proc = await spawn_pi(_settings(), cwd="/tmp/session", tools=False)
    assert proc.alive
    args = created["args"]
    assert isinstance(args, tuple)
    assert "--mode" in args
    assert created["cwd"] == "/tmp/session"


async def test_stdio_on_host_follows_isolation() -> None:
    none = load_isolation("none")
    microvm = load_isolation("microvm")
    assert none.stdio_on_host is True
    assert microvm.stdio_on_host is False
    servers = await start_mcp_stdio_tools(
        [
            {
                "type": "mcp",
                "server_label": "playwright",
                "transport": {"type": "stdio", "command": "npx", "args": []},
            }
        ],
        on_host=False,
    )
    assert servers == [
        McpStdioServer(server_label="playwright", command="npx", args=[], process=None)
    ]


def test_example_isolation_loads() -> None:
    backend = load_isolation("examples.isolation:ExampleIsolation")
    assert backend.name == "example"
    assert backend.needs_probe is False
    assert backend.stdio_on_host is True
