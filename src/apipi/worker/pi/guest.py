import contextlib
import fcntl
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tarfile
import threading
from collections.abc import Callable
from pathlib import Path

ARTIFACT_PORT = 53
WORKSPACE_PORT = 54
SESSION_PORT = 55
METRICS_PORT = 56
PUSH_PORT = 57
MAX_PUSH_HEADER = 32
PUBLISH_DIRS = ("outputs",)
SESSION_REL = ".apipi/pi-session.jsonl"
RNDADDENTROPY = 0x40085203


def _pi_args() -> list[str]:
    root = Path(os.environ.get("HOME", "/workspace"))
    path = root / ".apipi" / "pi-args"
    if path.is_file():
        raw = json.loads(path.read_text())
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            return raw
    return ["pi", "--mode", "rpc", "--no-session"]


def artifacts_tar_bytes(root: Path) -> bytes:
    buf = io.BytesIO()
    added = False
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for folder in PUBLISH_DIRS:
            src = root / folder
            if src.is_dir():
                tar.add(str(src), arcname=folder)
                added = True
    if not added:
        return b""
    return buf.getvalue()


def guest_sample(root: Path | None = None) -> dict[str, float]:
    workspace = root if root is not None else Path(os.environ.get("HOME", "/workspace"))
    mem_available = 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                mem_available = float(line.split()[1]) * 1024.0
                break
    except OSError:
        pass
    load_1 = 0.0
    with contextlib.suppress(OSError, IndexError, ValueError):
        load_1 = float(Path("/proc/loadavg").read_text().split()[0])
    used = 0.0
    avail = 0.0
    try:
        stats = os.statvfs(workspace)
        avail = float(stats.f_bavail * stats.f_frsize)
        used = float((stats.f_blocks - stats.f_bfree) * stats.f_frsize)
    except OSError:
        pass
    return {
        "mem_available_bytes": mem_available,
        "load_1": load_1,
        "workspace_used_bytes": used,
        "workspace_avail_bytes": avail,
    }


def guest_sample_bytes(root: Path) -> bytes:
    return (json.dumps(guest_sample(root)) + "\n").encode()


def session_file_bytes(root: Path) -> bytes:
    path = root / SESSION_REL
    if not path.is_file():
        return b""
    return path.read_bytes()


def workspace_tar_bytes(root: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if rel == ".apipi" or rel.startswith(".apipi/"):
                continue
            tar.add(str(path), arcname=rel)
    return buf.getvalue()


def unpack_push_tar(data: bytes, root: Path) -> int:
    """Write each file of a pushed tar that is missing under `root`.

    A file that exists stays as it is. Names outside `root` and names
    under `.apipi/` are skipped. Returns the number of written files.
    """
    written = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for info in tar.getmembers():
            if not info.isfile() or info.name.startswith("/"):
                continue
            parts = Path(info.name).parts
            if not parts or ".." in parts or parts[0] == ".apipi":
                continue
            target = root / info.name
            if target.exists():
                continue
            handle = tar.extractfile(info)
            if handle is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(handle.read())
            written += 1
    return written


def read_push(conn: socket.socket) -> bytes:
    """Read one push: a line with the size and then that many tar bytes."""
    header = b""
    while not header.endswith(b"\n"):
        chunk = conn.recv(1)
        if not chunk or len(header) >= MAX_PUSH_HEADER:
            raise OSError("bad push header")
        header += chunk
    size = int(header)
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(min(65536, size - len(data)))
        if not chunk:
            raise OSError("push ended early")
        data.extend(chunk)
    return bytes(data)


def handle_push(conn: socket.socket, root: Path) -> None:
    try:
        unpack_push_tar(read_push(conn), root)
    except (OSError, ValueError, tarfile.TarError) as exc:
        conn.sendall(f"ERR {type(exc).__name__}\n".encode())
        return
    conn.sendall(b"OK\n")


def _serve_push(port: int) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(8)
    root = Path(os.environ.get("HOME", "/workspace"))
    while True:
        conn, _ = sock.accept()
        try:
            handle_push(conn, root)
        except OSError:
            pass
        finally:
            conn.close()


def _serve_tar(port: int, build: Callable[[Path], bytes]) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(8)
    root = Path(os.environ.get("HOME", "/workspace"))
    while True:
        conn, _ = sock.accept()
        try:
            conn.sendall(build(root))
        except OSError:
            pass
        finally:
            conn.close()


def _serve_artifacts(port: int) -> None:
    _serve_tar(port, artifacts_tar_bytes)


def _serve_workspace(port: int) -> None:
    _serve_tar(port, workspace_tar_bytes)


def _serve_session(port: int) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(8)
    root = Path(os.environ.get("HOME", "/workspace"))
    while True:
        conn, _ = sock.accept()
        try:
            conn.sendall(session_file_bytes(root))
        except OSError:
            pass
        finally:
            conn.close()


def _serve_rpc(args: list[str], port: int) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(1)
    conn, _ = sock.accept()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        env=os.environ,
    )
    stdin = proc.stdin
    stdout = proc.stdout
    if stdin is None or stdout is None:
        conn.close()
        raise SystemExit("pi pipes missing")

    def to_pi() -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                stdin.write(data)
                stdin.flush()
        except OSError:
            pass
        with contextlib.suppress(OSError):
            stdin.close()

    threading.Thread(target=to_pi, daemon=True).start()
    try:
        fd = stdout.fileno()
        while True:
            data = os.read(fd, 65536)
            if not data:
                break
            conn.sendall(data)
    except OSError:
        pass
    proc.wait()


def _run_setup() -> None:
    root = Path(os.environ.get("HOME", "/workspace"))
    script = root / ".apipi" / "setup.sh"
    done = root / ".apipi" / "setup.done"
    if not script.is_file() or done.is_file():
        return
    result = subprocess.run(
        ["/bin/sh", str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    log = root / ".apipi" / "setup.log"
    log.write_text((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        sys.stderr.write(log.read_text())
        raise SystemExit("environment setup failed")
    done.write_text("ok\n")


def _workspace() -> Path:
    return Path(os.environ.get("HOME", "/workspace"))


def _seed_rng() -> None:
    path = _workspace() / ".apipi" / "random"
    if not path.is_file():
        return
    data = path.read_bytes()
    if len(data) < 64:
        return
    payload = struct.pack(f"ii{len(data)}s", len(data) * 8, len(data), data)
    try:
        with open("/dev/urandom", "wb") as rng:
            fcntl.ioctl(rng, RNDADDENTROPY, payload)
    except OSError:
        with contextlib.suppress(OSError), open("/dev/urandom", "wb") as rng:
            rng.write(data)


def _exec_shell() -> None:
    os.chdir(_workspace())
    os.execvp("sh", ["sh", "-i"])


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    port = 52
    if args:
        port = int(args[0])
    _seed_rng()
    _run_setup()
    if (_workspace() / ".apipi" / "shell").is_file():
        _exec_shell()
    threading.Thread(
        target=_serve_artifacts, args=(ARTIFACT_PORT,), daemon=True
    ).start()
    threading.Thread(
        target=_serve_workspace, args=(WORKSPACE_PORT,), daemon=True
    ).start()
    threading.Thread(target=_serve_session, args=(SESSION_PORT,), daemon=True).start()
    threading.Thread(
        target=_serve_tar, args=(METRICS_PORT, guest_sample_bytes), daemon=True
    ).start()
    threading.Thread(target=_serve_push, args=(PUSH_PORT,), daemon=True).start()
    _serve_rpc(_pi_args(), port)


if __name__ == "__main__":
    main()
