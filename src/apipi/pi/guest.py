import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
from pathlib import Path

ARTIFACT_PORT = 53


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
    src = root / "artifacts"
    if not src.is_dir():
        return b""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(str(src), arcname="artifacts")
    return buf.getvalue()


def _serve_artifacts(port: int) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(8)
    root = Path(os.environ.get("HOME", "/tmp/workspace"))
    while True:
        conn, _ = sock.accept()
        try:
            conn.sendall(artifacts_tar_bytes(root))
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
    _serve_rpc(_pi_args(), port)


if __name__ == "__main__":
    main()
