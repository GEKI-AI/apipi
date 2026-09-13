import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
from collections.abc import Callable
from pathlib import Path

ARTIFACT_PORT = 53
WORKSPACE_PORT = 54
PUBLISH_DIRS = ("artifacts", "outputs")


def _start_mcp() -> None:
    labels = os.environ.get("APIPI_MCP_STDIO")
    if not labels:
        return
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    for index, _label in enumerate(labels.split(",")):
        prefix = f"APIPI_MCP_STDIO_{index}"
        command = env.get(f"{prefix}_COMMAND")
        if not command:
            continue
        raw_args = env.get(f"{prefix}_ARGS", "")
        args = raw_args.split("\x1f") if raw_args else []
        subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )


def _pi_args() -> list[str]:
    root = Path(os.environ.get("HOME", "/tmp/workspace"))
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


def _serve_tar(port: int, build: Callable[[Path], bytes]) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(8)
    root = Path(os.environ.get("HOME", "/tmp/workspace"))
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
        stderr=subprocess.DEVNULL,
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
        while True:
            data = stdout.read(65536)
            if not data:
                break
            conn.sendall(data)
    except OSError:
        pass
    proc.wait()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    port = 52
    if args:
        port = int(args[0])
    _start_mcp()
    threading.Thread(
        target=_serve_artifacts, args=(ARTIFACT_PORT,), daemon=True
    ).start()
    threading.Thread(
        target=_serve_workspace, args=(WORKSPACE_PORT,), daemon=True
    ).start()
    _serve_rpc(_pi_args(), port)


if __name__ == "__main__":
    main()
