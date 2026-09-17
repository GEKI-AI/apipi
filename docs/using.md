# Using the API

This walkthrough matches
[OpenAI's Agents API quickstart](https://developers.openai.com/api/docs/guides/agents-api/quickstart?lang=python):
create a session, stream events, send a follow-up, then delete the
session. `environment.type` `openai_hosted` is a local folder next to
Pi (OpenAI's name for that field).

## Prerequisites

Have the gateway running ([Install](install.md)): `apipi serve` is
enough locally (SQLite at `.apipi/apipi.db`). Live turns need Pi on `PATH` and
`OPENAI_BASE_URL` on the gateway process (the **model** host that Pi
calls). The client bearer is the model key unless
`OPENAI_API_KEY_OVERWRITE` is set. `agent.model` must exist on that
host. The gateway always sends a short platform prompt, then
`agent.instructions` when those are set. See [config](config.md#pi).

In a second shell, point the official client at the gateway. For this
script, `OPENAI_BASE_URL` is the ApiPi gateway and `OPENAI_API_KEY` is
the bearer the gateway will accept. Default auth hashes that bearer
into a tenant id. The same key always maps to the same tenant. See
[auth](auth.md).

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

Isolation `none` is enough for this tutorial. Production usually uses
`APIPI_RUN_MODE=microvm`. A size `L` browser capture is
`examples/browser_screenshot.py`.

## 1. Run a task

Save the example as `examples/openai_sdk.py`, or run the copy in this
repo. The request creates a session with an inline agent, uses the
local sandbox, submits a coding task, and streams progress. Those
instructions reach the model through Pi, not only the agent JSON.

```python
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

    with OpenAI(
        api_key=api_key,
        base_url=os.environ.get("OPENAI_BASE_URL", "http://localhost:8000/v1"),
    ) as client:
        with client.beta.agents.sessions.with_streaming_response.create(
            agent={
                "model": "gpt-4.1",
                "instructions": "Write clean code, run it, and report the actual output.",
            },
            environment={"type": "openai_hosted"},
            input=(
                "Create tree.py, a Python script that prints a readable tree of the "
                "files in the current directory. Run it and show me the output."
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
```

The create arguments are the same as OpenAI's snippet: an inline
`agent`, `environment={"type": "openai_hosted"}`, a string `input`, and
`stream=True`. Set `base_url` and `api_key` so the client talks to this
gateway. The example prints SSE `data:` lines. The official SDK's typed
event objects expect OpenAI-hosted fields this API does not emit. The
stream stays open across `idle`, so the script stops after the first
turn outcome.

Run it:

```
uv run --with openai python examples/openai_sdk.py
```

## 2. Follow progress

The terminal shows one JSON object per public event. On a successful
live run, the agent creates `tree.py`, executes it, and reports a
directory tree. Other files and output depend on the sandbox.

Look for `agent.session.turn.completed`, then check the reported
execution result. A completed turn does not guarantee every tool
succeeded. Events ending in `turn.failed`, `turn.cancelled`, or
`session.failed` indicate failure or cancellation. `agent.session.idle`
alone does not mean success. If the stream disconnects early, retrieve
the session and its saved items before retrying. Event types are listed
in [API](api.md).

## 3. Continue the session

Save the `session_id` from the events. Send a follow-up such as “Add a
maximum-depth option to `tree.py`, run it, and show me the output.”
Open the event stream before sending follow-up input so you do not miss
early events.

This API accepts `POST /v1/agents/sessions/{session_id}/events` in two
shapes. Official SDK helpers send a nested `events` list with one
`agent.session.input.message` whose `input` holds `input_text`. Curl
and existing clients can send the flat body with `type` and `text` or
`content`. Unknown fields return an error.

```
curl -X POST "$OPENAI_BASE_URL/agents/sessions/$SESSION_ID/events" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"type":"agent.session.input.message","text":"Add a maximum-depth option to tree.py, run it, and show me the output."}'
```

Subscribe with `GET /v1/agents/sessions/{session_id}/events?stream=true`
before that POST if you want the follow-up as SSE.

## 4. Clean up

Keep the session for more tasks, or delete it when you are done. The
hosted workspace lasts until `APIPI_SANDBOX_TTL_OPENAI_HOSTED`,
or until you delete the session. Files under `outputs/` are published
when a turn completes. See
[Concepts](concepts.md).

Replace the illustrative session id with the id you saved.

```
curl -X DELETE "$OPENAI_BASE_URL/agents/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $OPENAI_API_KEY"
```

The response is `{"id": "…", "deleted": true}`.

## Next

- [Concepts](concepts.md) for agents, sessions, files, and artifacts.
- [API](api.md) for routes, events, and compatibility.
- [Environments](environments.md) for the local directory, `none`, and
  `self_hosted`.
- [Tools and skills](tools.md) for function tools, MCP, and `SKILL.md`.
- [Auth](auth.md) for bearers and tenants.
