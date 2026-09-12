# Examples

MCP configs and a small OpenAI Python SDK script. Keys come from the
environment, not from these files.

| File | What |
| --- | --- |
| [openai_sdk.py](openai_sdk.py) | Official OpenAI Python client against this API |
| [tavily.yaml](tavily.yaml) | Web search (Tavily hosted MCP) |
| [playwright.yaml](playwright.yaml) | Browser (Playwright MCP, headless) |

Skills are `SKILL.md` directories on the computer. Point at them with
`environment.capability_directories`. See [docs/tools.md](../docs/tools.md).

## OpenAI Python SDK

[openai_sdk.py](openai_sdk.py) uses `AsyncOpenAI` with `base_url` pointed
at this gateway. It creates an agent, opens a session with
`environment={"type": "none"}` (no computer), and reads the turn that
ran from that input. It only sends fields this API implements, so the
SDK does not add `multi_agent`, `vault_ids`, or other unknown keys that
would return `400`.

The `openai` package is not an ApiPi dependency. Install it for this
script only:

```
uv run --with openai python examples/openai_sdk.py
```

The gateway must already be running. Jail is the default when `bwrap`,
`pasta`, and cgroup v2 are present. Operators without those tools
should serve with host mode. `microvm` starts when `/dev/kvm`,
Firecracker, jailer, guest images, `ip`, and `iptables` are present;
otherwise serve exits. Send a bearer the client will send:

```
APIPI_RUN_MODE=host apipi serve
```

Default auth accepts any non-empty bearer and hashes it into a tenant
id. Set the same value on the client. For this script,
`OPENAI_BASE_URL` is the ApiPi gateway, not the model host that Pi
uses. If you omit it, the default is `http://localhost:8000/v1`.

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
uv run --with openai python examples/openai_sdk.py
```

The product [README](../README.md) has a shorter sketch of the same
client.
