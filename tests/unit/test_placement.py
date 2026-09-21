import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from apipi.config import Settings
from apipi.worker.hub import WorkerConnection, WorkerHub, _run_command
from apipi.worker.placement import placement_for, worker_accepts


def test_chat_session_kind_always_chat() -> None:
    assert (
        placement_for(
            environment={"type": "openai_hosted"},
            metadata={"apipi.session_kind": "chat"},
            env_none="microvm",
        )
        == "chat"
    )
    assert (
        placement_for(
            environment={"type": "none"},
            metadata={"apipi.session_kind": "chat"},
            env_none="reject",
        )
        == "chat"
    )


@pytest.mark.parametrize("env_none", ["chat", "microvm"])
def test_env_none_follows_config(env_none: str) -> None:
    assert (
        placement_for(
            environment={"type": "none"},
            metadata={},
            env_none=env_none,
        )
        == env_none
    )


def test_env_none_reject() -> None:
    assert (
        placement_for(
            environment={"type": "none"},
            metadata={},
            env_none="reject",
        )
        is None
    )


@pytest.mark.parametrize("env_type", ["openai_hosted", "hosted", "self_hosted"])
def test_computer_is_microvm(env_type: str) -> None:
    assert (
        placement_for(
            environment={"type": env_type},
            metadata={},
            env_none="chat",
        )
        == "microvm"
    )


def test_worker_accepts_same_mode() -> None:
    assert worker_accepts("chat", "chat")
    assert worker_accepts("microvm", "microvm")
    assert not worker_accepts("microvm", "chat")
    assert not worker_accepts("none", "microvm")
    assert worker_accepts("none", "chat")


async def test_turn_start_rejects_microvm_on_none() -> None:
    execution = MagicMock()
    execution.settings.run_mode = "none"
    execution.store = None
    execution.hub = None
    execution.run_turn = AsyncMock()
    await _run_command(
        execution,
        "turn.start",
        uuid.uuid4(),
        uuid.uuid4(),
        {"text": "hi", "run_mode": "microvm"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_not_called()


async def test_turn_start_rejects_chat_on_microvm() -> None:
    execution = MagicMock()
    execution.settings.run_mode = "microvm"
    execution.store = None
    execution.hub = None
    execution.run_turn = AsyncMock()
    await _run_command(
        execution,
        "turn.start",
        uuid.uuid4(),
        uuid.uuid4(),
        {"text": "hi", "run_mode": "chat"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_not_called()


async def test_turn_start_none_accepts_chat() -> None:
    execution = MagicMock()
    execution.settings.run_mode = "none"
    execution.run_turn = AsyncMock()
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    await _run_command(
        execution,
        "turn.start",
        tenant_id,
        session_id,
        {"text": "hi", "run_mode": "chat"},
        request_id=None,
        api_key=None,
        key_id=None,
        user_id=None,
    )
    execution.run_turn.assert_awaited_once()


def test_pick_filters_run_mode() -> None:
    hub = WorkerHub(
        Settings(
            database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
            run_mode="none",
            microvm_mem_mib=512,
        )
    )
    chat = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=8,
        memory_mb=4096,
        run_mode="chat",
    )
    microvm = WorkerConnection(
        worker_id=uuid.uuid4(),
        generation=1,
        websocket=MagicMock(),
        capacity=8,
        memory_mb=8192,
        run_mode="microvm",
    )
    hub._conns[chat.worker_id] = chat
    hub._conns[microvm.worker_id] = microvm
    assert hub.pick(run_mode="chat") is chat
    assert hub.pick(run_mode="microvm") is microvm
    assert hub.pick(run_mode="none") is None
