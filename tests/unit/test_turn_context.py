import time
import uuid

import pytest
from pydantic import ValidationError

from apipi.config import Settings
from apipi.store.blobs import (
    NS_FILES,
    ObjectStoreError,
    file_object_id,
    local_object_path,
)
from apipi.store.engine import Store
from apipi.worker.pi.dirs import sessions_root
from apipi.worker.pi.pool import PiPool
from apipi.worker.turn_context import (
    MAX_COMMAND_BYTES,
    CommandTooLarge,
    ContextBytes,
    check_command_size,
    check_context_op,
    parse_turn_context,
    redact_context,
    summarize_context,
)


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


async def test_summarize_context_has_no_secrets() -> None:
    summary = summarize_context(_context())
    assert summary["mcp_servers"] == ["mock"]
    dumped = str(summary)
    assert "secret" not in dumped
    assert "token=abc" not in dumped


async def test_local_ref_path_guard(settings: Settings) -> None:
    from apipi.services.turn_context import fetch_ref_bytes, local_ref_path

    root = sessions_root(settings)
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
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.worker.pi.artifacts as artifacts
    from apipi.worker.pi.artifacts import reap_workspaces

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("reaper must not touch the database")

    monkeypatch.setattr(artifacts, "get_session", _boom)
    monkeypatch.setattr(artifacts, "get_agent", _boom)
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
    await reap_workspaces(
        settings,
        store,
        pool,
        ttl_overrides={
            str(fresh_id): (3600.0, now),
            str(stale_id): (300.0, now - 3600.0),
        },
    )
    assert (root / str(tenant_id) / str(fresh_id)).is_dir()
    assert not (root / str(tenant_id) / str(stale_id)).exists()


async def test_reap_workspaces_override_none_ttl_keeps_workspace(
    settings: Settings, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apipi.worker.pi.artifacts as artifacts
    from apipi.worker.pi.artifacts import reap_workspaces

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("reaper must not touch the database")

    monkeypatch.setattr(artifacts, "get_session", _boom)
    monkeypatch.setattr(artifacts, "get_agent", _boom)
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    directory = sessions_root(settings) / str(tenant_id) / str(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    pool = PiPool(settings)
    await reap_workspaces(
        settings,
        store,
        pool,
        ttl_overrides={str(session_id): (None, time.time() - 10_000.0)},
    )
    assert directory.is_dir()
