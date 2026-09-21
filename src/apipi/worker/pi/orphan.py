import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from apipi.gateway.logutil import log_event

log = logging.getLogger("apipi.worker.pi")

WORKER_PID_ENV = "APIPI_WORKER_PID"
HOST_PI_ENV = "APIPI_HOST_PI"


def host_pi_stamp(*, worker_pid: int | None = None) -> dict[str, str]:
    return {
        HOST_PI_ENV: "1",
        WORKER_PID_ENV: str(os.getpid() if worker_pid is None else worker_pid),
    }


def _pid_running(pid: int, proc_root: str) -> bool:
    path = Path(proc_root) / str(pid) / "stat"
    try:
        rest = path.read_text().split(")")[-1].split()
    except OSError:
        return False
    return bool(rest) and rest[0] not in {"Z", "X"}


def _environ(pid: int, proc_root: str) -> dict[str, str]:
    try:
        raw = (Path(proc_root) / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, _, val = item.partition(b"=")
        with contextlib.suppress(UnicodeDecodeError):
            out[key.decode()] = val.decode()
    return out


def _stat_fields(pid: int, proc_root: str) -> list[str] | None:
    try:
        return (Path(proc_root) / str(pid) / "stat").read_text().split(")")[-1].split()
    except OSError:
        return None


def orphan_pids(
    *,
    proc_root: str = "/proc",
    my_pid: int | None = None,
    skip_pids: set[int] | None = None,
) -> list[int]:
    me = os.getpid() if my_pid is None else my_pid
    skip = skip_pids if skip_pids is not None else set()
    found: list[int] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid == me or pid in skip:
            continue
        raw = _environ(pid, proc_root).get(WORKER_PID_ENV)
        if raw is None or not raw.isdigit():
            continue
        parent = int(raw)
        if parent == me or _pid_running(parent, proc_root):
            continue
        found.append(pid)
    return found


def _kill(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


def _killpg(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _is_leader(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        return os.getpgid(pid) == pid
    except ProcessLookupError:
        return False


def _kill_matching(
    proc_root: str,
    sig: int,
    *,
    pgid: int | None = None,
    ppid: int | None = None,
) -> None:
    mypid = os.getpid()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == mypid:
            continue
        fields = _stat_fields(pid, proc_root)
        if fields is None or len(fields) < 3:
            continue
        try:
            got_ppid = int(fields[1])
            got_pgrp = int(fields[2])
        except ValueError:
            continue
        if (pgid is not None and got_pgrp == pgid) or (
            ppid is not None and got_ppid == ppid
        ):
            _kill(pid, sig)


async def sweep_host_orphans(
    *,
    proc_root: str = "/proc",
    my_pid: int | None = None,
    skip_pids: set[int] | None = None,
    grace: float = 0.2,
) -> int:
    pids = orphan_pids(proc_root=proc_root, my_pid=my_pid, skip_pids=skip_pids)
    if not pids:
        return 0
    log_event(
        log,
        logging.INFO,
        "worker orphan sweep",
        event="worker.orphan.reaped",
        count=len(pids),
        pids=pids,
    )
    for pid in pids:
        if _is_leader(pid):
            _killpg(pid, signal.SIGTERM)
        else:
            _kill(pid, signal.SIGTERM)
    await asyncio.sleep(grace)
    for pid in pids:
        if _is_leader(pid):
            _killpg(pid, signal.SIGKILL)
            _kill_matching(proc_root, signal.SIGKILL, pgid=pid)
        else:
            _kill(pid, signal.SIGKILL)
            _kill_matching(proc_root, signal.SIGKILL, ppid=pid)
    return len(pids)
