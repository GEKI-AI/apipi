import time
import uuid

import pytest
from pydantic import ValidationError

from apipi.common.dirs import sessions_root, store_root
from apipi.common.errors import ObjectStoreError
from apipi.common.objects import NS_FILES, local_object_path
from apipi.config import Settings
from apipi.protocol import (
    MAX_COMMAND_BYTES,
    CommandTooLarge,
    ContextBytes,
    check_command_size,
    check_context_op,
    parse_turn_context,
    redact_context,
    summarize_context,
)
from apipi.store.blobs import file_object_id
from apipi.worker.pi.pool import PiPool


def _context(**overrides: object) -> dict:
    base: dict = {
        "session": {
            "environment": {"type": "none"},
            "metadata": {},
            "required_actions": [],
            "status": "idle",
            "user_id": None,
            "org_id": None,
            "key_id": "key",
            "agent_id": None,
            "idle_ttl_seconds": 900.0,
        },
        "agent": {
            "model": "test",
            "instructions": "Be helpful.",
            "function_tools": [],
            "metadata": {},
            "builtin_tools": "off",
            "codemode": "off",
            "thinking": None,
        },
        "model": {"base_url": None, "api_key": "secret-key"},
        "mcp": [
            {
                "server_label": "mock",
                "server_url": "https://mcp.example/session?token=abc",
                "headers": {"Authorization": "Bearer vault-secret"},
                "allowed_tools": [],
            }
        ],
        "files": [],
        "skills": [],
        "pi_session": {"present": False},
    }
    base.update(overrides)
    return base


async def test_parse_turn_context_ok() -> None:
    parsed = parse_turn_context(_context())
    assert parsed.session.environment == {"type": "none"}
    assert parsed.mcp[0].server_label == "mock"
    assert parsed.model.api_key == "secret-key"


async def test_parse_turn_context_rejects_bytes() -> None:
    raw = _context()
    raw["files"] = [
        {
            "path": "a.txt",
            "object_id": "files/a",
            "url": None,
            "local_path": None,
            "data": b"bytes must not travel here",
        }
    ]
    with pytest.raises((ContextBytes, ValidationError)):
        parse_turn_context(raw)


async def test_parse_turn_context_rejects_raw_bytes() -> None:
    raw = _context()
    assert isinstance(raw["session"], dict)
    raw["session"]["environment"] = {"type": "none", "blob": b"nope"}
    with pytest.raises(ContextBytes):
        parse_turn_context(raw)


async def test_check_command_size_limit() -> None:
    raw = _context()
    payload = {"context": raw}
    assert check_command_size(payload) > 0
    big = _context()
    assert isinstance(big["agent"], dict)
    big["agent"]["instructions"] = "x" * (MAX_COMMAND_BYTES + 1)
    with pytest.raises(CommandTooLarge):
        check_command_size({"context": big})
    with pytest.raises(CommandTooLarge):
        check_context_op("turn.start", {"context": big})
    check_context_op("session.stop", {"context": big})


async def test_redact_context_removes_secrets() -> None:
    raw = _context()
    redacted = redact_context(raw)
    assert redacted["model"]["api_key"] == "..."
    assert redacted["mcp"][0]["headers"] == {"Authorization": "..."}
    assert "token=abc" not in redacted["mcp"][0]["server_url"]
    assert redacted["mcp"][0]["server_url"].startswith("https://mcp.example/session")
    assert raw["model"]["api_key"] == "secret-key"
    assert raw["mcp"][0]["headers"] == {"Authorization": "Bearer vault-secret"}


async def test_env_credentials_are_redacted_and_hidden_from_repr() -> None:
    raw = _context(
        env_credentials=[
            {
                "credential_id": "cred",
                "secret_name": "GITHUB_TOKEN",
                "secret_value": "ghp-live",
                "allowed_hosts": ["github.com"],
                "git_username": None,
            }
        ]
    )
    parsed = parse_turn_context(raw)
    assert parsed.env_credentials[0].secret_value == "ghp-live"
    assert "ghp-live" not in repr(parsed)
    assert "secret-key" not in repr(parsed)
    redacted = redact_context(raw)
    assert redacted["env_credentials"][0]["secret_value"] == "..."
    assert redacted["env_credentials"][0]["secret_name"] == "GITHUB_TOKEN"
    assert raw["env_credentials"][0]["secret_value"] == "ghp-live"
    summary = summarize_context(raw)
    assert summary["env_credential_count"] == 1
    assert "ghp-live" not in str(summary)


def test_guest_env_deny_list_covers_the_worker() -> None:
    from apipi.common.guest_env import GUEST_ENV_NEVER, reserved_secret_name
    from apipi.worker.pi import microvm

    assert microvm.GUEST_ENV_NEVER is GUEST_ENV_NEVER
    assert all(reserved_secret_name(name) for name in microvm._GUEST_FILE_KEYS)


async def test_summarize_context_has_no_secrets() -> None:
    summary = summarize_context(_context())
    assert summary["mcp_servers"] == ["mock"]
    dumped = str(summary)
    assert "secret" not in dumped
    assert "token=abc" not in dumped


async def test_local_ref_path_guard(settings: Settings) -> None:
    from apipi.worker.turn_context import fetch_ref_bytes, local_ref_path

    root = store_root(settings)
    namespace_path = local_object_path(
        root, NS_FILES, file_object_id(uuid.uuid4(), "f")
    )
    namespace_path.parent.mkdir(parents=True, exist_ok=True)
    namespace_path.write_bytes(b"hello")
    relative = str(namespace_path.relative_to(root))
    assert (
        await fetch_ref_bytes({"url": None, "local_path": relative}, settings)
        == b"hello"
    )
    with pytest.raises(ObjectStoreError):
        local_ref_path(settings, "../escape")
    with pytest.raises(ObjectStoreError):
        await fetch_ref_bytes({"url": None, "local_path": "../escape"}, settings)


async def test_reap_workspaces_uses_ttl_overrides_without_db(
    settings: Settings,
) -> None:
    from apipi.worker.pi.artifacts import reap_workspaces

    tenant_id = uuid.uuid4()
    fresh_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    root = sessions_root(settings)
    for session_id in (fresh_id, stale_id):
        directory = root / str(tenant_id) / str(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "note.txt").write_text("x", encoding="utf-8")
    pool = PiPool(settings)
    now = time.time()
    wiped = await reap_workspaces(
        settings,
        pool,
        ttl_overrides={
            str(fresh_id): (3600.0, now, "openai_hosted"),
            str(stale_id): (300.0, now - 3600.0, "openai_hosted"),
        },
    )
    assert (root / str(tenant_id) / str(fresh_id)).is_dir()
    assert not (root / str(tenant_id) / str(stale_id)).exists()
    assert wiped == [str(stale_id)]


async def test_reap_workspaces_override_none_ttl_keeps_workspace(
    settings: Settings,
) -> None:
    from apipi.worker.pi.artifacts import reap_workspaces

    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    directory = sessions_root(settings) / str(tenant_id) / str(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    pool = PiPool(settings)
    await reap_workspaces(
        settings,
        pool,
        ttl_overrides={
            str(session_id): (None, time.time() - 10_000.0, "openai_hosted")
        },
    )
    assert directory.is_dir()


async def test_reap_workspaces_override_skips_non_hosted_env(
    settings: Settings,
) -> None:
    from apipi.worker.pi.artifacts import reap_workspaces

    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    directory = sessions_root(settings) / str(tenant_id) / str(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    pool = PiPool(settings)
    wiped = await reap_workspaces(
        settings,
        pool,
        ttl_overrides={str(session_id): (300.0, time.time() - 3600.0, "none")},
    )
    assert directory.is_dir()
    assert wiped == []


async def test_reap_workspaces_sees_late_context_entries(
    settings: Settings,
) -> None:
    from apipi.worker.pi.artifacts import reap_workspaces

    tenant_id = uuid.uuid4()
    early_id = uuid.uuid4()
    late_id = uuid.uuid4()
    root = sessions_root(settings)
    early_dir = root / str(tenant_id) / str(early_id)
    early_dir.mkdir(parents=True, exist_ok=True)
    pool = PiPool(settings)
    live: dict[str, tuple[float | None, float, str | None]] = {
        str(early_id): (None, time.time(), "openai_hosted")
    }
    await reap_workspaces(settings, pool, ttl_overrides=live)
    assert early_dir.is_dir()
    late_dir = root / str(tenant_id) / str(late_id)
    late_dir.mkdir(parents=True, exist_ok=True)
    live[str(late_id)] = (300.0, time.time() - 3600.0, "openai_hosted")
    wiped = await reap_workspaces(settings, pool, ttl_overrides=live)
    assert early_dir.is_dir()
    assert not late_dir.exists()
    assert wiped == [str(late_id)]


def _local_execution(settings: Settings):
    from apipi.common.event_bus import EventHub
    from apipi.worker.execution import LocalExecution
    from apipi.worker.fake_harness import FakeHarness
    from apipi.worker.outbox import Outbox

    return LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=FakeHarness(),
        hub=EventHub(),
        outbox=Outbox(),
    )


def _worker_context(
    seconds: float | None = 900.0, env_type: str | None = "openai_hosted"
) -> dict:
    return {
        "session": {
            "environment": {"type": env_type} if env_type else {},
            "idle_ttl_seconds": seconds,
        }
    }


async def test_reap_loop_passes_live_mapping_and_evicts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.worker.execution as execution_module

    seen: dict = {}

    async def _stub(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(execution_module, "reap_workspace_loop", _stub)
    execution = _local_execution(settings)
    await execution.reap_workspace_loop()
    assert seen["ttl_overrides"] is execution._context_ttl
    session_id = uuid.uuid4()
    execution.note_context_ttl(session_id, _worker_context())
    assert str(session_id) in seen["ttl_overrides"]
    seen["on_wiped"](str(session_id))
    assert str(session_id) not in execution._context_ttl


async def test_turn_end_refreshes_idle_clock(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.worker.execution as execution_module

    async def _stub(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(execution_module, "run_turn", _stub)
    clock = iter([100.0, 200.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    execution = _local_execution(settings)
    session_id = uuid.uuid4()
    await execution.run_turn(
        uuid.uuid4(),
        session_id,
        "hi",
        turn_context=_worker_context(),  # type: ignore[arg-type]
    )
    assert execution._context_ttl[str(session_id)] == (900.0, 200.0, "openai_hosted")


async def test_turn_error_still_refreshes_idle_clock(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.worker.execution as execution_module

    async def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("turn failed")

    monkeypatch.setattr(execution_module, "run_turn", _boom)
    clock = iter([100.0, 200.0])
    monkeypatch.setattr(time, "time", lambda: next(clock))
    execution = _local_execution(settings)
    session_id = uuid.uuid4()
    with pytest.raises(RuntimeError):
        await execution.run_turn(
            uuid.uuid4(),
            session_id,
            "hi",
            turn_context=_worker_context(),  # type: ignore[arg-type]
        )
    assert execution._context_ttl[str(session_id)] == (900.0, 200.0, "openai_hosted")


async def test_teardown_forgets_context(settings: Settings) -> None:
    execution = _local_execution(settings)
    session_id = uuid.uuid4()
    execution.note_context_ttl(session_id, _worker_context())
    assert str(session_id) in execution._context_ttl
    await execution.teardown(session_id)
    assert str(session_id) not in execution._context_ttl
