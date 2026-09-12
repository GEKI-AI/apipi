import json
import sys


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
                "assistantMessageEvent": {"type": "text_delta", "delta": text},
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_end", "content": text},
            },
            {"type": "agent_end", "messages": []},
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
