import base64
import json
import os
import sys

import httpx

_DONE = {
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
    "agent.session.failed",
}
_FAILED = {
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
    "agent.session.failed",
}

_SOURCE = "mango\napple\nbanana\n"
_PATH = "/workspace/fruits.txt"
_OUTPUT = "outputs/fruits-sorted.txt"


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY to a bearer the gateway will accept.")
    base = os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1").rstrip("/")
    model = os.environ.get("APIPI_MODEL", "gpt-4.1")
    dest = os.environ.get("APIPI_OUTPUT", "fruits-sorted.txt")
    headers = {"Authorization": f"Bearer {api_key}"}
    body = {
        "agent": {
            "model": model,
            "instructions": (
                "Read workspace files. Write the result under outputs/ only. "
                "Do not install packages."
            ),
        },
        "environment": {
            "type": "openai_hosted",
            "sandbox_size": "S",
            "files": [
                {
                    "type": "inline",
                    "path": _PATH,
                    "data": base64.b64encode(_SOURCE.encode()).decode(),
                }
            ],
        },
        "input": (
            "Read fruits.txt. Sort the names alphabetically, one per line, "
            f"and write only that list to {_OUTPUT}."
        ),
        "stream": True,
    }
    session_id = None
    outcome = None
    with httpx.Client(timeout=600.0) as client:
        with client.stream(
            "POST",
            f"{base}/agents/sessions",
            headers=headers,
            json=body,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"request failed: {response.status_code} {response.read().decode()}"
                )
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ")
                print(payload, flush=True)
                event = json.loads(payload)
                kind = event.get("type")
                if kind == "agent.session.created":
                    session_id = event.get("data", {}).get("id") or event.get(
                        "session_id"
                    )
                if kind in _DONE:
                    outcome = kind
                    break
        if outcome in _FAILED or session_id is None:
            raise SystemExit(f"turn did not complete: {outcome}")
        listed = client.get(
            f"{base}/agents/sessions/{session_id}/artifacts",
            headers=headers,
        )
        listed.raise_for_status()
        rows = listed.json().get("data") or []
        match = next((row for row in rows if row.get("path") == _OUTPUT), None)
        if match is None:
            raise SystemExit(f"no artifact {_OUTPUT}")
        content = client.get(
            f"{base}/agents/sessions/{session_id}/artifacts/{match['id']}/content",
            headers=headers,
        )
        content.raise_for_status()
        with open(dest, "wb") as handle:
            handle.write(content.content)
        text = content.content.decode()
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()
