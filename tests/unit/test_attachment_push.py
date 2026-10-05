import asyncio
import io
import socket
import tarfile
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import pytest

from apipi.config import ConfigError, Settings
from apipi.protocol import TurnStartCommandPayload
from apipi.services.files import attachment_name, free_attachment_path
from apipi.worker.pi.guest import handle_push, unpack_push_stream
from apipi.worker.pi.microvm import (
    VSOCK_PUSH_PORT,
    push_tar_bytes,
    push_workspace_files,
)
from apipi.worker.pi.pool import PiPool
from apipi.worker.runtime import _prompt_text, _push_attachments, _user_item_content
from apipi.worker.turn_context import attached_line, size_text
from apipi.workerhub.commands import command_features

_PART = {
    "type": "file",
    "file_id": "file-1",
    "filename": "report.xlsx",
    "mime_type": "application/vnd.ms-excel",
    "size_bytes": 245760,
    "model_input": "workspace",
    "path": "attachments/report.xlsx",
}


def _tar(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("report.xlsx", "report.xlsx"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\notes.md", "notes.md"),
        ("..", "file"),
        ("", "file"),
        ("a\x00b\nc.txt", "abc.txt"),
        ("\ud800a.txt", "a.txt"),
        ("a\u200bb\u202e.txt", "ab.txt"),
        (".env", ".env"),
    ],
)
def test_attachment_name(filename: str, expected: str) -> None:
    assert attachment_name(filename) == expected


def test_long_attachment_name_keeps_its_extension() -> None:
    name = attachment_name("é" * 300 + ".xlsx")
    assert name.endswith(".xlsx")
    assert len(name.encode()) <= 200


def test_free_attachment_path() -> None:
    taken = {"attachments/report.xlsx", "attachments/report (2).xlsx"}
    assert free_attachment_path("report.xlsx", set()) == "attachments/report.xlsx"
    assert free_attachment_path("report.xlsx", taken) == "attachments/report (3).xlsx"
    assert (
        free_attachment_path("Makefile", {"attachments/Makefile"})
        == "attachments/Makefile (2)"
    )
    assert free_attachment_path(".env", {"attachments/.env"}) == "attachments/.env (2)"


def test_attached_line_and_size_text() -> None:
    assert attached_line(_PART) == "Attached: attachments/report.xlsx (xlsx, 240 KB)"
    assert (
        attached_line({**_PART, "path": "attachments/data", "mime_type": "text/csv"})
        == "Attached: attachments/data (text/csv, 240 KB)"
    )
    assert (
        attached_line(
            {
                **_PART,
                "path": "attachments/.env",
                "mime_type": "application/octet-stream",
            }
        )
        == "Attached: attachments/.env (file, 240 KB)"
    )
    assert size_text(512) == "512 B"
    assert size_text(1536 * 1024) == "1.5 MB"
    assert size_text(3 * 1024**3) == "3.0 GB"


def test_prompt_and_item_carry_the_path() -> None:
    parts = [{"type": "input_text", "text": "sum column C"}, _PART]
    assert _prompt_text("sum column C", parts, []) == (
        "sum column C\nAttached: attachments/report.xlsx (xlsx, 240 KB)"
    )
    assert _user_item_content("sum column C", parts) == [
        {"type": "input_text", "text": "sum column C"},
        {
            "type": "input_file",
            "file_id": "file-1",
            "filename": "report.xlsx",
            "path": "attachments/report.xlsx",
        },
    ]


def test_workspace_part_on_the_wire_has_no_store_reference() -> None:
    body = TurnStartCommandPayload.model_validate({"parts": [_PART]})
    assert body.to_wire()["parts"] == [_PART]


def test_session_files_need_the_feature() -> None:
    context = {"session": {}, "session_files": [{"path": "attachments/a"}]}
    assert "session_files" in command_features(
        {"op": "turn.start", "payload": {"parts": [_PART]}}
    )
    assert "session_files" in command_features(
        {"op": "turn.continue", "payload": {"context": context}}
    )
    assert "session_files" not in command_features(
        {"op": "turn.start", "payload": {"context": {"session_files": []}}}
    )


def test_guest_replaces_pushed_files(tmp_path: Path) -> None:
    (tmp_path / "attachments").mkdir()
    (tmp_path / "attachments" / "data.csv").write_bytes(b"agent content")
    data = _tar(
        {
            "attachments/new.txt": b"new",
            "attachments/data.csv": b"user content",
            "../escape.txt": b"x",
            "/abs.txt": b"x",
            ".apipi/env": b"x",
        }
    )
    assert unpack_push_stream(io.BytesIO(data), tmp_path) == 2
    assert (tmp_path / "attachments" / "new.txt").read_bytes() == b"new"
    assert (tmp_path / "attachments" / "data.csv").read_bytes() == b"user content"
    assert sorted(path.name for path in (tmp_path / "attachments").iterdir()) == [
        "data.csv",
        "new.txt",
    ]
    assert not (tmp_path.parent / "escape.txt").exists()
    assert not (tmp_path / ".apipi").exists()


def test_guest_push_answers_ok_or_err(tmp_path: Path) -> None:
    data = push_tar_bytes([("attachments/a.txt", b"a")])
    host, guest = socket.socketpair()
    with host, guest:
        host.sendall(f"{len(data)}\n".encode() + data)
        handle_push(guest, tmp_path)
        assert host.recv(16) == b"OK\n"
        host.sendall(f"{len(data)}\n".encode() + data[:100])
        host.shutdown(socket.SHUT_WR)
        handle_push(guest, tmp_path)
        assert host.recv(32).startswith(b"ERR")
    assert (tmp_path / "attachments" / "a.txt").read_bytes() == b"a"
    bad_host, bad_guest = socket.socketpair()
    with bad_host, bad_guest:
        bad_host.sendall(b"nonsense\n")
        handle_push(bad_guest, tmp_path)
        assert bad_host.recv(32).startswith(b"ERR")


async def test_host_pushes_files_over_the_vsock_port(tmp_path: Path) -> None:
    path = tmp_path / "vsock.sock"
    root = tmp_path / "workspace"
    root.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    seen: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        with conn:
            line = b""
            while not line.endswith(b"\n"):
                line += conn.recv(1)
            seen.append(line)
            conn.sendall(f"OK {VSOCK_PUSH_PORT}\n".encode())
            handle_push(conn, root)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        await push_workspace_files(
            path, [("attachments/report.xlsx", b"sheet")], timeout=5
        )
    finally:
        thread.join(timeout=5)
        server.close()
    assert seen == [f"CONNECT {VSOCK_PUSH_PORT}\n".encode()]
    assert (root / "attachments" / "report.xlsx").read_bytes() == b"sheet"


async def test_host_push_fails_without_ok(tmp_path: Path) -> None:
    path = tmp_path / "vsock.sock"

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(f"OK {VSOCK_PUSH_PORT}\n".encode())
        await writer.drain()
        size = int(await reader.readline())
        await reader.readexactly(size)
        writer.write(b"ERR TarError\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handler, path=str(path))
    async with server:
        with pytest.raises(ConfigError):
            await push_workspace_files(path, [("attachments/a", b"a")], timeout=2)


class _Proc:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.pushed: list[list[tuple[str, bytes]]] = []

    async def push_files(self, files: list[tuple[str, bytes]]) -> None:
        if self.fail:
            raise ConfigError("microvm guest did not take the files")
        self.pushed.append(files)


class _Pool:
    def __init__(self, proc: Any) -> None:
        self.proc = proc
        self.killed: list[tuple[uuid.UUID, str]] = []

    async def settled(self, session_id: uuid.UUID) -> Any:
        return self.proc

    async def kill(self, session_id: uuid.UUID, *, reason: str = "session") -> None:
        self.killed.append((session_id, reason))


async def test_new_attachments_are_pushed_into_a_running_guest() -> None:
    session_id = uuid.uuid4()
    files = [("attachments/a.txt", b"a")]
    proc = _Proc()
    pool = cast(Any, _Pool(proc))
    assert await _push_attachments(pool, session_id, files)
    assert await _push_attachments(pool, session_id, [])
    assert await _push_attachments(None, session_id, files)
    assert proc.pushed == [files]
    failing = cast(Any, _Pool(_Proc(fail=True)))
    assert not await _push_attachments(failing, session_id, files)
    assert failing.killed == []
    idle = cast(Any, _Pool(None))
    assert await _push_attachments(idle, session_id, files)


async def test_settled_waits_for_a_boot_in_flight(settings: Settings) -> None:
    pool = PiPool(settings)
    session_id = uuid.uuid4()
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    pool._inflight[session_id] = future
    waiter = asyncio.create_task(pool.settled(session_id))
    await asyncio.sleep(0)
    assert not waiter.done()
    future.set_exception(RuntimeError("boot failed"))
    pool._inflight.pop(session_id)
    assert await waiter is None


async def test_host_push_times_out_when_the_guest_hangs(tmp_path: Path) -> None:
    path = tmp_path / "vsock.sock"

    async def handler(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.readline()
        writer.write(f"OK {VSOCK_PUSH_PORT}\n".encode())
        await writer.drain()
        await asyncio.sleep(5)

    server = await asyncio.start_unix_server(handler, path=str(path))
    async with server:
        with pytest.raises(TimeoutError):
            await push_workspace_files(path, [("attachments/a", b"a")], timeout=0.3)


async def test_kill_without_the_hook_keeps_the_lease(settings: Settings) -> None:
    hooked: list[uuid.UUID] = []

    async def on_kill(session_id: uuid.UUID, _proc: Any, _release: bool) -> None:
        hooked.append(session_id)

    pool = PiPool(settings, on_kill=on_kill)
    first, second = uuid.uuid4(), uuid.uuid4()
    await pool.kill(first, reason="push_failed", hook=False)
    await pool.kill(second, reason="push_failed")
    assert hooked == [second]
