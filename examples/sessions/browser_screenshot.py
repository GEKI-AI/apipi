import json
import os

from openai import OpenAI

_DONE = {
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
    "agent.session.failed",
}


def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Set OPENAI_API_KEY to a bearer the gateway will accept.")
    model = os.environ.get("APIPI_MODEL", "gpt-4.1")
    url = os.environ.get("APIPI_PAGE_URL", "https://example.com")

    with OpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
    ) as client:
        with client.beta.agents.sessions.with_streaming_response.create(
            agent={
                "model": model,
                "instructions": (
                    "Use the Playwright MCP tools. Do not install packages. "
                    "Put screenshots under outputs/."
                ),
            },
            environment={"type": "openai_hosted", "sandbox_size": "L"},
            input=(
                f"Open {url} in the browser. Tell me the page title and the "
                "main heading. Save a screenshot as outputs/page.png."
            ),
            stream=True,
        ) as response:
            if response.status_code != 200:
                raise RuntimeError(f"request failed: {response.status_code}")
            for line in response.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line.removeprefix("data: ")
                print(payload, flush=True)
                event = json.loads(payload)
                if event.get("type") in _DONE:
                    break


if __name__ == "__main__":
    main()
