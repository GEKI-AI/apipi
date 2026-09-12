import json
import sys

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


def main() -> None:
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
        events = [
            {"type": "agent_start"},
            {"type": "turn_start"},
            {
                "type": "message_update",
                "usage": PI_USAGE,
                "assistantMessageEvent": {"type": "text_delta", "delta": text},
            },
            {
                "type": "message_update",
                "usage": PI_USAGE,
                "assistantMessageEvent": {"type": "text_end", "content": text},
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
