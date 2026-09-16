import json
import sys
from pathlib import Path

PI_USAGE = {
    "input": 5,
    "output": 8,
    "cacheRead": 1,
    "cacheWrite": 2,
    "totalTokens": 16,
    "cost": {
        "input": 0.1,
        "output": 0.2,
        "cacheRead": 0.0,
        "cacheWrite": 0.0,
        "total": 0.3,
    },
    "prompt": "secret-prompt",
}


def _session_path() -> Path | None:
    args = sys.argv[1:]
    if "--session" not in args:
        return None
    index = args.index("--session")
    if index + 1 >= len(args):
        return None
    return Path(args[index + 1])


def _load_history(path: Path | None) -> list[str]:
    if path is None or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _save_history(path: Path | None, history: list[str]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history), encoding="utf-8")


def main() -> None:
    session = _session_path()
    history = _load_history(session)
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if command.get("type") != "prompt":
            continue
        message = command.get("message")
        text = message if isinstance(message, str) and message else "ok"
        history.append(text)
        _save_history(session, history)
        Path("keep.txt").write_text(text, encoding="utf-8")
        reply = " ".join(history)
        events = [
            {"type": "agent_start"},
            {"type": "turn_start"},
            {
                "type": "message_update",
                "usage": PI_USAGE,
                "assistantMessageEvent": {"type": "text_delta", "delta": reply},
            },
            {
                "type": "message_update",
                "usage": PI_USAGE,
                "assistantMessageEvent": {"type": "text_end", "content": reply},
            },
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "usage": PI_USAGE}],
            },
            {"type": "agent_settled"},
        ]
        for event in events:
            sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.write(
            json.dumps({"type": "response", "command": "prompt", "success": True})
            + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
