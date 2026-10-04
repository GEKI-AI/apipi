import base64
import shutil
import tarfile
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from apipi.common.event_bus import InMemoryEventBus
from apipi.config import Settings
from apipi.env.setup import SetupError
from apipi.worker.execution import LocalExecution
from apipi.worker.fake_harness import FakeHarness
from apipi.worker.outbox import Outbox
from apipi.worker.pi.microvm import write_workspace_image
from apipi.worker.pi.pool import PiPool
from apipi.worker.sink import OutboxSink
from apipi.worker.turn_context import materialize_workspace_files

STORE = {"obj-a": b"original a", "obj-b": b"original b"}


class CountingStore:
    def __init__(self) -> None:
        self.fetches: list[str] = []

    async def fetch(self, ref: Mapping[str, Any], settings: Settings) -> bytes:
        object_id = str(ref["object_id"])
        self.fetches.append(object_id)
        return STORE[object_id]


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> CountingStore:
    store = CountingStore()
    monkeypatch.setattr("apipi.worker.turn_context.fetch_ref_bytes", store.fetch)
    return store


def _ref(path: str, object_id: str) -> dict[str, Any]:
    return {
        "path": path,
        "object_id": object_id,
        "url": None,
        "local_path": f"files/{object_id}",
        "size_bytes": len(STORE[object_id]),
        "content_type": "text/plain",
    }


def _context(workspace: Path) -> dict[str, Any]:
    inline = base64.b64encode(b"original inline").decode()
    return {
        "session": {
            "environment": {
                "type": "openai_hosted",
                "directory": str(workspace),
                "files": [
                    {"type": "inline", "path": "inputs/inline.txt", "data": inline},
                    {"type": "file_id", "file_id": "file-a", "path": "inputs/a.txt"},
                    {"type": "file_id", "file_id": "file-b", "path": "inputs/b.txt"},
                ],
            },
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
            "builtin_tools": "on",
            "codemode": "off",
            "thinking": None,
        },
        "model": {"base_url": None, "api_key": "secret-key"},
        "mcp": [],
        "files": [_ref("inputs/a.txt", "obj-a"), _ref("inputs/b.txt", "obj-b")],
        "skills": [],
        "pi_session": {"present": False},
    }


def _execution(settings: Settings) -> LocalExecution:
    return LocalExecution(
        settings,
        pool=PiPool(settings),
        harness=FakeHarness(),
        hub=InMemoryEventBus(),
        outbox=Outbox(),
    )


async def _turn(
    execution: LocalExecution, workspace: Path, session_id: uuid.UUID
) -> None:
    tenant_id = uuid.uuid4()
    outbox = execution.outbox
    assert outbox is not None
    await execution.run_turn(
        tenant_id,
        session_id,
        "hello",
        turn_context=_context(workspace),
        sink=OutboxSink(outbox, tenant_id, session_id),
    )
    assert not any(
        item["type"] == "event"
        and item["payload"].get("type") == "agent.session.environment.failed"
        for item in outbox.pending(session_id)
    )


async def test_materialize_fetches_only_missing_paths(
    settings: Settings, fake_store: CountingStore, tmp_path: Path
) -> None:
    workspace = tmp_path / "session"
    (workspace / "inputs").mkdir(parents=True)
    (workspace / "inputs" / "a.txt").write_bytes(b"edited")
    refs = [_ref("inputs/a.txt", "obj-a"), _ref("/workspace/inputs/b.txt", "obj-b")]
    files = await materialize_workspace_files(refs, settings, workspace)
    assert files == [("/workspace/inputs/b.txt", b"original b")]
    assert fake_store.fetches == ["obj-b"]
    assert await materialize_workspace_files(refs, settings, None) == []
    assert fake_store.fetches == ["obj-b"]


async def test_materialize_rejects_escape_without_fetch(
    settings: Settings, fake_store: CountingStore, tmp_path: Path
) -> None:
    workspace = tmp_path / "session"
    workspace.mkdir()
    with pytest.raises(SetupError, match="inside the workspace"):
        await materialize_workspace_files(
            [_ref("../outside.txt", "obj-a")], settings, workspace
        )
    assert fake_store.fetches == []


async def test_none_edits_survive_until_workspace_is_deleted(
    settings: Settings, fake_store: CountingStore
) -> None:
    session_id = uuid.uuid4()
    workspace = Path(settings.sessions_dir or "") / "tenant" / str(session_id)
    execution = _execution(settings)
    await _turn(execution, workspace, session_id)
    inputs = workspace / "inputs"
    assert (inputs / "a.txt").read_bytes() == b"original a"
    assert (inputs / "b.txt").read_bytes() == b"original b"
    assert (inputs / "inline.txt").read_bytes() == b"original inline"
    assert sorted(fake_store.fetches) == ["obj-a", "obj-b"]

    (inputs / "a.txt").write_bytes(b"agent edit")
    (inputs / "inline.txt").write_bytes(b"agent inline edit")
    fake_store.fetches.clear()
    await _turn(execution, workspace, session_id)
    assert (inputs / "a.txt").read_bytes() == b"agent edit"
    assert (inputs / "inline.txt").read_bytes() == b"agent inline edit"
    assert fake_store.fetches == []

    (inputs / "b.txt").unlink()
    await _turn(execution, workspace, session_id)
    assert (inputs / "b.txt").read_bytes() == b"original b"
    assert (inputs / "a.txt").read_bytes() == b"agent edit"
    assert fake_store.fetches == ["obj-b"]

    shutil.rmtree(workspace)
    fake_store.fetches.clear()
    await _turn(execution, workspace, session_id)
    assert (inputs / "a.txt").read_bytes() == b"original a"
    assert (inputs / "b.txt").read_bytes() == b"original b"
    assert (inputs / "inline.txt").read_bytes() == b"original inline"
    assert sorted(fake_store.fetches) == ["obj-a", "obj-b"]


def _image_files(workspace: Path, dest: Path) -> dict[str, bytes]:
    write_workspace_image(
        dest, cwd=str(workspace), env={}, pi_args=["pi", "--mode", "rpc"]
    )
    found: dict[str, bytes] = {}
    with tarfile.open(dest, mode="r") as tar:
        for member in tar.getmembers():
            name = member.name.removeprefix("./")
            if not name.startswith("inputs/") or not member.isfile():
                continue
            handle = tar.extractfile(member)
            assert handle is not None
            found[name] = handle.read()
    return found


async def test_microvm_boot_writes_all_files_and_running_guest_fetches_none(
    settings: Settings, fake_store: CountingStore, tmp_path: Path
) -> None:
    microvm = settings.model_copy(update={"run_mode": "microvm"})
    session_id = uuid.uuid4()
    workspace = Path(settings.sessions_dir or "") / "tenant" / str(session_id)
    execution = _execution(microvm)
    spawned: list[dict[str, Any]] = []

    async def _fake_get(session_id: uuid.UUID, **kwargs: Any) -> None:
        spawned.append(kwargs)

    execution.pool.get = _fake_get  # ty: ignore[invalid-assignment]
    await execution.boot_hosted(
        uuid.uuid4(), session_id, turn_context=_context(workspace)
    )
    assert spawned and spawned[0]["cwd"] == str(workspace)
    assert sorted(fake_store.fetches) == ["obj-a", "obj-b"]
    expected = {
        "inputs/a.txt": b"original a",
        "inputs/b.txt": b"original b",
        "inputs/inline.txt": b"original inline",
    }
    assert _image_files(workspace, tmp_path / "boot.tar") == expected

    fake_store.fetches.clear()
    await _turn(execution, workspace, session_id)
    assert fake_store.fetches == []

    shutil.rmtree(workspace)
    spawned.clear()
    await execution.boot_hosted(
        uuid.uuid4(), session_id, turn_context=_context(workspace)
    )
    assert spawned
    assert sorted(fake_store.fetches) == ["obj-a", "obj-b"]
    assert _image_files(workspace, tmp_path / "reboot.tar") == expected
