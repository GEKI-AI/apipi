import json
import sys


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line.startswith("{"):
            print("expected newline-delimited json", file=sys.stderr)
            raise SystemExit(1)
        msg = json.loads(line)
        method = msg.get("method")
        if method == "notifications/initialized":
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fixture", "version": "0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{"name": "echo", "description": "Echo", "inputSchema": {}}]
            }
        else:
            print(f"unexpected method {method}", file=sys.stderr)
            raise SystemExit(1)
        body = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        sys.stdout.write(json.dumps(body) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
