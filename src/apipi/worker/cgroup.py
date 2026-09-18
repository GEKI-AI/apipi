from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")


def jailer_cgroup_dir(vm_id: str, *, root: Path = _CGROUP_ROOT) -> Path | None:
    candidates = (
        root / "jailer" / "firecracker" / vm_id,
        root / "system.slice" / f"jailer-{vm_id}.scope",
    )
    for path in candidates:
        if (path / "memory.current").is_file():
            return path
    return None


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    if raw in {"", "max"}:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cpu_usage_seconds(path: Path) -> float | None:
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("usage_usec "):
            try:
                return int(line.split()[1]) / 1_000_000.0
            except (IndexError, ValueError):
                return None
    return None


def read_cgroup(vm_id: str, *, root: Path = _CGROUP_ROOT) -> dict[str, float] | None:
    directory = jailer_cgroup_dir(vm_id, root=root)
    if directory is None:
        return None
    memory = _read_int(directory / "memory.current")
    limit = _read_int(directory / "memory.max")
    cpu = _cpu_usage_seconds(directory / "cpu.stat")
    if memory is None and limit is None and cpu is None:
        return None
    return {
        "memory_bytes": float(memory or 0),
        "memory_limit_bytes": float(limit or 0),
        "cpu_seconds": float(cpu or 0.0),
    }
