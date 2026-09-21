import os
from pathlib import Path

_PROC_ROOT = Path("/proc")


def _kb_to_bytes(line: str) -> int:
    parts = line.split()
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1]) * 1024
    except ValueError:
        return 0


def _statm_rss(path: Path) -> int:
    try:
        parts = path.read_text().split()
        pages = int(parts[1])
    except (OSError, IndexError, ValueError):
        return 0
    page = os.sysconf("SC_PAGE_SIZE") or 4096
    return pages * page


def read_rss_pss(pid: int, *, proc_root: Path = _PROC_ROOT) -> tuple[int, int]:
    directory = proc_root / str(pid)
    rollup = directory / "smaps_rollup"
    try:
        text = rollup.read_text()
    except OSError:
        return _statm_rss(directory / "statm"), 0
    rss = 0
    pss = 0
    for line in text.splitlines():
        if line.startswith("Rss:"):
            rss = _kb_to_bytes(line)
        elif line.startswith("Pss:"):
            pss = _kb_to_bytes(line)
    return rss, pss


def _pgrp_members(pgid: int, proc_root: Path) -> list[int]:
    found: list[int] = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            rest = (proc_root / name / "stat").read_text().split(")")[-1].split()
            if int(rest[2]) == pgid:
                found.append(int(name))
        except (OSError, IndexError, ValueError):
            continue
    return found


def read_group_rss_pss(pid: int, *, proc_root: Path = _PROC_ROOT) -> tuple[int, int]:
    pgid = pid
    try:
        rest = (proc_root / str(pid) / "stat").read_text().split(")")[-1].split()
        pgid = int(rest[2])
    except (OSError, IndexError, ValueError):
        pass
    members = _pgrp_members(pgid, proc_root)
    if not members:
        members = [pid]
    rss = 0
    pss = 0
    for member in members:
        r, p = read_rss_pss(member, proc_root=proc_root)
        rss += r
        pss += p
    return rss, pss
