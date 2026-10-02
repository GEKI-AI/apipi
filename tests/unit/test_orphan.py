import os
from pathlib import Path

from apipi.worker.pi.orphan import (
    WORKER_PID_ENV,
    host_pi_stamp,
    orphan_pids,
)
from apipi.worker.pi.proc import pi_env


def _fake_proc(
    root: Path, pid: int, *, environ: bytes, stat: str | None = None
) -> None:
    proc = root / str(pid)
    proc.mkdir(parents=True)
    (proc / "environ").write_bytes(environ)
    if stat is None:
        stat = f"{pid} (sleep) S 1 {pid} {pid} 0 0 0\n"
    (proc / "stat").write_text(stat)


def test_host_pi_stamp_uses_current_pid() -> None:
    stamp = host_pi_stamp()
    assert stamp["APIPI_HOST_PI"] == "1"
    assert stamp[WORKER_PID_ENV] == str(os.getpid())


def test_pi_env_stamps_worker_pid(tmp_path: Path) -> None:
    from apipi.config import Settings

    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
    )
    env = pi_env(settings)
    assert env["APIPI_HOST_PI"] == "1"
    assert env[WORKER_PID_ENV] == str(os.getpid())


def test_orphan_pids_reaps_dead_worker(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _fake_proc(
        proc,
        4242,
        environ=f"{WORKER_PID_ENV}=9999\0APIPI_HOST_PI=1\0".encode(),
    )
    assert orphan_pids(proc_root=str(proc), my_pid=1) == [4242]


def test_orphan_pids_skips_live_worker(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _fake_proc(
        proc, 100, environ=b"PATH=/bin\0", stat="100 (worker) S 1 100 100 0 0 0\n"
    )
    _fake_proc(
        proc,
        4242,
        environ=f"{WORKER_PID_ENV}=100\0".encode(),
    )
    assert orphan_pids(proc_root=str(proc), my_pid=1) == []


def test_orphan_pids_skips_current_worker_children(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    me = 7
    _fake_proc(
        proc,
        4242,
        environ=f"{WORKER_PID_ENV}={me}\0".encode(),
    )
    assert orphan_pids(proc_root=str(proc), my_pid=me) == []


def test_orphan_pids_skips_unstamped(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _fake_proc(proc, 4242, environ=b"PATH=/bin\0")
    assert orphan_pids(proc_root=str(proc), my_pid=1) == []


def test_orphan_pids_honors_skip(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _fake_proc(
        proc,
        4242,
        environ=f"{WORKER_PID_ENV}=9999\0".encode(),
    )
    assert orphan_pids(proc_root=str(proc), my_pid=1, skip_pids={4242}) == []
