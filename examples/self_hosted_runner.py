import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _workspace_path(root: Path, raw: object) -> Path | None:
    rel = raw if isinstance(raw, str) else ""
    rel = rel.strip() or "."
    if rel.startswith("/") or ".." in Path(rel).parts:
        return None
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _list_names(root: Path, raw: object) -> list[str]:
    prefix = raw if isinstance(raw, str) else ""
    if prefix in {".", "./"}:
        prefix = ""
    names: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = Path(dirpath, name).relative_to(root).as_posix()
            if rel.startswith(prefix):
                names.append(rel)
    return sorted(names)


def _handle(root: Path, message: dict[str, Any]) -> dict[str, Any]:
    req_id = message.get("id")
    msg_type = message.get("type")
    if msg_type == "ping":
        return {"id": req_id, "ok": True, "type": "pong"}
    if msg_type == "close":
        return {"id": req_id, "ok": True, "type": "close"}
    if msg_type == "exec":
        command = message.get("command")
        if not isinstance(command, str) or command == "":
            return {"id": req_id, "ok": False, "error": "command required"}
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"id": req_id, "ok": False, "error": str(exc)}
        return {
            "id": req_id,
            "ok": True,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    if msg_type == "list":
        return {"id": req_id, "ok": True, "names": _list_names(root, message.get("path"))}
    path = _workspace_path(root, message.get("path"))
    if path is None:
        return {"id": req_id, "ok": False, "error": "not found"}
    if msg_type == "read":
        if not path.is_file():
            return {"id": req_id, "ok": False, "error": "not found"}
        return {
            "id": req_id,
            "ok": True,
            "content": path.read_bytes().decode("utf-8", errors="replace"),
        }
    if msg_type == "write":
        content = message.get("content")
        if not isinstance(content, str):
            return {"id": req_id, "ok": False, "error": "content required"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return {"id": req_id, "ok": True}
    if msg_type == "edit":
        if not path.is_file():
            return {"id": req_id, "ok": False, "error": "not found"}
        old = message.get("old_text")
        new = message.get("new_text")
        if not isinstance(old, str) or not isinstance(new, str):
            return {"id": req_id, "ok": False, "error": "old_text and new_text required"}
        text = path.read_text()
        if old not in text:
            return {"id": req_id, "ok": False, "error": "not found"}
        path.write_text(text.replace(old, new, 1))
        return {"id": req_id, "ok": True}
    if msg_type == "artifact":
        if not path.is_file():
            return {"id": req_id, "ok": False, "error": "not found"}
        return {"id": req_id, "ok": True}
    return {"id": req_id, "ok": False, "error": "unknown type"}


def _ws_url(base: str, environment_id: str) -> str:
    url = base.rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = "ws://" + url.removeprefix("http://")
    return f"{url}/environments/{environment_id}"


async def _run(root: Path, url: str, key: str) -> None:
    import websockets

    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "key": key}))
        raw = await ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode()
        ack = json.loads(raw)
        if not isinstance(ack, dict) or not ack.get("ok"):
            raise SystemExit(f"hello failed: {ack}")
        async for incoming in ws:
            if isinstance(incoming, bytes):
                incoming = incoming.decode()
            message = json.loads(incoming)
            if not isinstance(message, dict):
                continue
            await ws.send(json.dumps(_handle(root, message)))
            if message.get("type") == "close":
                return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Attach a local directory as an ApiPi self_hosted computer."
    )
    parser.add_argument(
        "--dir",
        default=os.environ.get("APIPI_RUNNER_DIR", "."),
        help="Workspace directory (default: APIPI_RUNNER_DIR or cwd)",
    )
    args = parser.parse_args()
    env_id = os.environ.get("APIPI_ENVIRONMENT_ID")
    key = os.environ.get("APIPI_ENVIRONMENT_KEY")
    if not env_id or not key:
        raise SystemExit("Set APIPI_ENVIRONMENT_ID and APIPI_ENVIRONMENT_KEY.")
    root = Path(args.dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1")
    asyncio.run(_run(root, _ws_url(base, env_id), key))


if __name__ == "__main__":
    main()
