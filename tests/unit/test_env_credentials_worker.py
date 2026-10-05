import subprocess
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from apipi.config import ConfigError, Settings
from apipi.env.setup import NetworkPolicy, SetupError, tap_policy_from
from apipi.protocol import (
    FEATURE_ENV_CREDENTIALS,
    SUPPORTED_FEATURES,
    ContextEnvCredential,
)
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc, spawn_pi
from apipi.workerhub.commands import command_features, require_credential_isolation

HELPER = Path(__file__).parents[2] / "src/apipi/worker/pi/git-credential.sh"
PH = "apipi-secret-" + "c" * 32


def _helper(tmp_path: Path, op: str, request: str) -> str:
    data = tmp_path / "git-credentials"
    data.write_text(
        f"github.com\tx-access-token\t{PH}\ngit.example.com\tapipi bot\tph-2\n"
    )
    result = subprocess.run(
        ["sh", str(HELPER), str(data), op],
        input=request,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_helper_answers_get_for_known_host(tmp_path: Path) -> None:
    out = _helper(tmp_path, "get", "protocol=https\nhost=GitHub.com\npath=a/b\n\n")
    assert out == f"username=x-access-token\npassword={PH}\n"
    for port in ("443", "8443"):
        ported = _helper(tmp_path, "get", f"protocol=https\nhost=github.com:{port}\n\n")
        assert ported == f"username=x-access-token\npassword={PH}\n"
    assert _helper(tmp_path, "get", "protocol=https\nhost=github.com:9000\n\n") == ""
    spaced = _helper(tmp_path, "get", "protocol=https\nhost=git.example.com\n")
    assert spaced == "username=apipi bot\npassword=ph-2\n"


@pytest.mark.parametrize(
    ("op", "payload"),
    [
        ("store", "protocol=https\nhost=github.com\nusername=a\npassword=b\n\n"),
        ("erase", "protocol=https\nhost=github.com\n\n"),
        ("get", "protocol=https\nhost=gitlab.com\n\n"),
        ("get", "protocol=http\nhost=github.com\n\n"),
        ("get", "protocol=https\n\n"),
    ],
)
def test_helper_stays_silent(tmp_path: Path, op: str, payload: str) -> None:
    assert _helper(tmp_path, op, payload) == ""


def test_helper_without_file_is_silent(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", str(HELPER), str(tmp_path / "missing"), "get"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""


def test_tap_policy_adds_credential_hosts_to_restricted() -> None:
    policy = NetworkPolicy(access="restricted", allowed_domains=("pypi.org",))
    tap = tap_policy_from(
        policy, gateway_allowlist=False, credential_hosts=("github.com",)
    )
    assert tap.mode == "restricted"
    assert tap.hosts == ("pypi.org", "github.com")
    enabled = tap_policy_from(
        None, gateway_allowlist=False, credential_hosts=("github.com",)
    )
    assert enabled.mode == "enabled"


def test_tap_policy_checks_credential_hosts_against_operator() -> None:
    with pytest.raises(SetupError, match=r"credential host api\.github\.com"):
        tap_policy_from(
            None,
            gateway_allowlist=True,
            gateway_hosts=("github.com",),
            credential_hosts=("github.com", "api.github.com"),
        )
    tap = tap_policy_from(
        None,
        gateway_allowlist=True,
        gateway_hosts=("github.com",),
        credential_hosts=("github.com",),
    )
    assert tap.hosts == ("github.com",)


def test_tap_policy_disabled_rejects_credentials() -> None:
    with pytest.raises(SetupError, match="network access"):
        tap_policy_from(
            NetworkPolicy(access="disabled"),
            gateway_allowlist=False,
            credential_hosts=("github.com",),
        )
    assert (
        tap_policy_from(NetworkPolicy(access="disabled"), gateway_allowlist=False).mode
        == "disabled"
    )


async def test_spawn_pi_refuses_credentials_without_microvm(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    with pytest.raises(ConfigError, match="isolation microvm"):
        await spawn_pi(
            settings,
            cwd=str(tmp_path),
            tools=True,
            env_type="openai_hosted",
            env_credentials=[
                ContextEnvCredential(
                    credential_id="c",
                    secret_name="T",
                    secret_value="v",
                    allowed_hosts=["github.com"],
                )
            ],
        )


def test_env_credentials_feature() -> None:
    assert FEATURE_ENV_CREDENTIALS in SUPPORTED_FEATURES
    credential = {
        "credential_id": "c",
        "secret_name": "T",
        "secret_value": "v",
        "allowed_hosts": ["github.com"],
    }
    for op in ("turn.start", "turn.continue", "sandbox.boot"):
        wire = {"op": op, "payload": {"context": {"env_credentials": [credential]}}}
        assert FEATURE_ENV_CREDENTIALS in command_features(wire)
        empty = {"op": op, "payload": {"context": {"env_credentials": []}}}
        assert FEATURE_ENV_CREDENTIALS not in command_features(empty)
    stop = {"op": "session.stop", "payload": {}}
    assert command_features(stop) == []


def _cred(value: str) -> ContextEnvCredential:
    return ContextEnvCredential(
        credential_id="c",
        secret_name="GITHUB_TOKEN",
        secret_value=value,
        allowed_hosts=["github.com"],
    )


async def test_pool_spawns_with_credentials_and_keeps_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = PiPool(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
        )
    )
    spawned: list[dict[str, Any]] = []

    class _Alive:
        alive = True
        broker = None

        async def stop(self) -> None:
            return None

    async def _spawn(*_args: object, **kwargs: Any) -> PiProc:
        spawned.append(kwargs)
        return cast(PiProc, _Alive())

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", _spawn)
    sid = uuid.uuid4()
    first = await pool.get(sid, cwd=None, tools=True, env_credentials=[_cred("one")])
    again = await pool.get(sid, cwd=None, tools=True, env_credentials=[_cred("two")])
    assert first is again
    assert len(spawned) == 1
    assert spawned[0]["env_credentials"][0].secret_value == "one"
    other = uuid.uuid4()
    await pool.get(other, cwd=None, tools=True)
    assert "env_credentials" not in spawned[1]


async def test_harness_forwards_credentials_to_the_pool() -> None:
    seen: list[dict[str, Any]] = []

    class _Proc:
        broker = None

        async def prompt(self, _text: str, **_kwargs: object) -> Any:
            yield {"type": "agent_settled", "success": True}

    class _Pool:
        async def get(self, *_args: object, **kwargs: Any) -> Any:
            seen.append(kwargs)
            return _Proc()

        def touch(self, session_id: object) -> None:
            del session_id

    harness = PiHarness(cast(Any, _Pool()))
    credentials = [_cred("v")]
    async for _event in harness.generate(
        "hi", session_id=uuid.uuid4(), env_credentials=credentials
    ):
        pass
    async for _event in harness.generate("hi", session_id=uuid.uuid4()):
        pass
    assert seen[0]["env_credentials"] is credentials
    assert "env_credentials" not in seen[1]


def _credential_wire(op: str = "turn.start") -> dict[str, Any]:
    return {
        "op": op,
        "payload": {
            "context": {
                "env_credentials": [
                    {
                        "credential_id": "c",
                        "secret_name": "T",
                        "secret_value": "value-123",
                        "allowed_hosts": ["github.com"],
                    }
                ]
            }
        },
    }


@pytest.mark.parametrize("run_mode", ["none", "package.mod:Class"])
def test_hub_refuses_credentials_for_workers_without_microvm(run_mode: str) -> None:
    from types import SimpleNamespace

    from apipi.common.errors import ApiError
    from apipi.protocol import SUPPORTED_FEATURES
    from apipi.workerhub.hub import WorkerHub

    hub = WorkerHub(
        Settings(database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi")
    )
    conn = SimpleNamespace(
        run_mode=run_mode, features=SUPPORTED_FEATURES, worker_id=uuid.uuid4()
    )
    with pytest.raises(ApiError) as refused:
        hub._enqueue(_credential_wire(), cast(Any, conn))
    assert refused.value.code == "credential_not_allowed"
    assert refused.value.status_code == 400
    assert run_mode in refused.value.message
    require_credential_isolation(_credential_wire(), "microvm")
    require_credential_isolation({"op": "turn.start", "payload": {}}, "none")


def test_credentials_prefer_microvm_workers() -> None:
    from unittest.mock import MagicMock

    from apipi.protocol import FEATURE_ENV_CREDENTIALS, SUPPORTED_FEATURES
    from apipi.workerhub.connection import WorkerConnection
    from apipi.workerhub.fleet import Candidate
    from apipi.workerhub.hub import able_candidates

    def candidate(run_mode: str) -> Candidate:
        conn = WorkerConnection(
            worker_id=uuid.uuid4(),
            generation=1,
            websocket=MagicMock(),
            capacity=8,
            memory_mb=8192,
            run_mode=run_mode,
            accepts=frozenset({"microvm"}),
            features=SUPPORTED_FEATURES,
        )
        return Candidate(
            worker_id=conn.worker_id,
            accepts=conn.accepts,
            images=frozenset(),
            arch="x86_64",
            draining=False,
            capacity=8,
            memory_mb=8192,
            leases=0,
            used_mem=0,
            conn=conn,
        )

    custom = candidate("package.mod:Class")
    microvm = candidate("microvm")
    assert able_candidates([custom, microvm], [FEATURE_ENV_CREDENTIALS]) == [microvm]
    assert able_candidates([custom, microvm], []) == [custom, microvm]
