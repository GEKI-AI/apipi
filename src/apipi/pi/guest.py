import json
import os
import socket
import subprocess
import sys
from pathlib import Path


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


def _serve_rpc(args: list[str], port: int) -> None:
    sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((socket.VMADDR_CID_ANY, port))
    sock.listen(1)
    conn, _ = sock.accept()
    os.dup2(conn.fileno(), 0)
    os.dup2(conn.fileno(), 1)
    os.execvpe(args[0], args, os.environ)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    port = 52
    if args:
        port = int(args[0])
    _start_mcp()
    _serve_rpc(_pi_args(), port)


if __name__ == "__main__":
    main()
