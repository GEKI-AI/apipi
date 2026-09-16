# OpenAI compatibility

ApiPi speaks the [OpenAI Agents API](https://developers.openai.com/api/docs/guides/agents-api)
shapes that official clients use. You bring the model host and the
isolation. This page is the contract: what those clients can do today,
what uses the same JSON with a different backend, and what returns an
error.

Request bodies are strict. An unknown JSON key is `400` with code
`unknown_field`. A known OpenAI field we have not implemented is `400`
with type `not_implemented`. Nothing extra is stored or ignored. Route
and field detail stays on [API](api.md).

Status in the tables:

| Status | Meaning |
| --- | --- |
| Same API | Official client calls work with the same request and response shapes. |
| Same shape, different backend | The JSON matches; the computer, model, or harness is ApiPi's. |
| Error | Extra key (`unknown_field`) or a named OpenAI field (`not_implemented`). |

The beta header `OpenAI-Beta: agents=v1` is accepted and ignored.

## Same for builders

Point the official OpenAI Python client at this gateway
(`OPENAI_BASE_URL=http://localhost:8000/v1` on the **client**, with a
bearer). Create an agent, open a session, stream events, send a
follow-up, cancel, and delete. `examples/openai_sdk.py` and
[Using the API](using.md) walk through that flow.

You can:

- Create, list, get, update, and delete agents. Fields are `name`,
  `model`, `instructions`, `metadata`, and `tools` (function, MCP HTTP,
  MCP stdio).
- Create a session with `agent` or `agent_id`, `environment`, `input`,
  `metadata`, and `stream`. A non-empty `input` starts the first turn.
- Stream with `GET /v1/agents/sessions/{id}/events?stream=true` and
  reconnect with `after_seq`.
- Send follow-ups with `POST /v1/agents/sessions/{id}/events`. Official
  SDK helpers (`sessions.events.create`) send a nested `events` list
  with one item. Curl can send the flat `{type, text|content, …}` body.
  Both mean the same thing. Message, cancel, and tool_result are
  supported. Send one shape, not both.
- List turns, items, and artifacts; fetch artifact bytes; export the
  transcript.
- List models with `GET /v1/models` when `APIPI_FORWARD_MODELS` is on
  (the default). That call proxies to your model host.
- Use function tools, MCP, and skills via
  `environment.capability_directories` (`SKILL.md` trees on the
  computer).
- Set `environment.type` to `openai_hosted` (default), `hosted` (alias),
  `none`, or `self_hosted`. On hosted computers, `packages` and
  `setup_commands` run before the first turn.
- Authenticate with `Authorization: Bearer` on every route except
  `/health` and `/metrics`.

`agent.model` must exist on the model host. Missing model is
`model_required`. Unknown id is `model_not_found`.

## Same shape, different backend

These fields and routes look like OpenAI's. The machine behind them is
yours.

| Topic | OpenAI | ApiPi |
| --- | --- | --- |
| `openai_hosted` / `hosted` | Cloud Linux sandbox | A local session directory next to Pi. In `microvm`, guest cwd is `/workspace`. OpenAI's field name, a folder on your machine. |
| Model | OpenAI-hosted models | `OPENAI_BASE_URL` on the **gateway** is the model host Pi calls. Clients use a different URL for this API. |
| Agent loop | OpenAI Codex | [Pi](https://pi.dev) over RPC |
| Isolation | OpenAI's managed sandbox | Run mode `none` or `microvm` ([run modes](run-modes.md)) |
| Auth | OpenAI account keys | A callback maps the bearer to a tenant. The gateway does not mint keys. See [auth](auth.md). |
| `self_hosted` | OpenAI `codex exec-server` | ApiPi runner WebSocket at `/v1/environments/{environment_id}`. Create returns `environment_id` and a one-time `key`. |
| Hosted files | Last until OpenAI's sandbox idle expiry | Last until `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour). Then Pi stops and `/workspace` is deleted. The next turn rebuilds skills, packages, and setup commands, and reloads the harness session cache. The session transcript stays. |
| `output_text.delta` | May be durable on their side | Live SSE only. Reconnect and export use `output_text.done` and items. |
| SSE events | Typed OpenAI stream objects | `{type, seq, data, …}`. Extra OpenAI fields such as `delta` at the top level are omitted. Use raw SSE / `with_streaming_response`. |

## HTTP routes

| Route | Status |
| --- | --- |
| `POST/GET /v1/agents`, `GET/POST/DELETE /v1/agents/{id}` | Same API (field subset) |
| `GET /v1/models` | Same API when forwarding is on; `not_implemented` `forward_models` when off |
| `POST/GET /v1/agents/sessions`, `GET/POST/DELETE /v1/agents/sessions/{id}` | Same API |
| `POST/GET /v1/agents/sessions/{id}/events` | Same API (nested `events` and flat body) |
| `GET /v1/agents/sessions/{id}/export` | Same API |
| `GET …/turns`, `GET …/items`, `GET/DELETE …/artifacts` | Same API |
| `GET /v1/usage` | ApiPi operator route (tokens and turn counts) |
| `WS /v1/environments/{environment_id}` | Same shape, different backend (ApiPi runner protocol) |
| `GET /health`, `GET /metrics` | ApiPi operator routes |
| `/v1/chat/completions` | Error (no such route) |
| `/v1/skills` hosted store | Error (skills are files on the computer) |
| ChatKit | Error (no such routes) |
| Vaults | `/v1/agents/vaults` and credentials. `static_bearer` only. GET omits token values. `mcp_oauth` is `not_implemented`. |

## Agent fields and tools

| Field or tool | Status |
| --- | --- |
| `name`, `model`, `instructions`, `metadata` | Same API |
| `tools` type `function` | Same API |
| `tools` type `mcp` with `server_url` | Same API (HTTP MCP) |
| `tools` type `mcp` with `command` | Same API (stdio MCP; ApiPi extension) |
| `multi_agent`, `tool_search`, `programmatic_tool_calling` | Error (`not_implemented`) |
| First-party `web_search` | Error; use MCP (example: Tavily) |
| First-party browser | Error; use MCP (example: Playwright) |
| Unknown JSON keys | Error (`unknown_field`) |

## Environment fields

| Field | Status |
| --- | --- |
| `type`: `openai_hosted`, `hosted`, `none`, `self_hosted` | Same shape, different backend for hosted; same API for `none` |
| `capability_directories` | Same API (skills on the computer) |
| `packages`, `setup_commands` | Same API on `openai_hosted` only; `400` on `none` or `self_hosted` |
| `files`, `env`, `network`, `environment_template_id`, `skills`, `plugins` | Error (`not_implemented`) |
| Unknown JSON keys | Error (`unknown_field`) |

## Lifecycle

| Piece | OpenAI | ApiPi |
| --- | --- | --- |
| Session / transcript | Durable on their side | Durable in SQLite or Postgres until you delete the session. Export is enough to leave. |
| Computer / files | Cloud sandbox, about an hour idle | Hosted directory until sandbox TTL (default 1 hour), then a fresh `/workspace`. `none` has no files. `self_hosted` files stay on the runner. |
| Idle Pi | Their sandbox runtime | `none` and `self_hosted`: `APIPI_IDLE_TTL` (default 15 minutes) stops Pi. Hosted computers use sandbox TTL. The session row stays. The next turn starts a new Pi and reloads the cached session file. |
| Artifacts | `/workspace/outputs` published on turn complete | `/workspace/outputs` copied to the host store on turn complete. Immutable. Downloadable after the workspace expires. |
| Follow-up affinity | OpenAI's fleet | API-only plus workers: any API replica. Combined `apipi serve`: sticky to the node that holds Pi. See [multiple nodes](scale.md). |

## Errors

| Case | Type | Code |
| --- | --- | --- |
| Extra JSON key | `invalid_request` | `unknown_field` |
| Known OpenAI field we skip | `not_implemented` | The field name (`multi_agent`, `files`, …) |
| Missing or bad bearer | `invalid_request` | `unauthorized` (`401`) |
| Id on another tenant | `invalid_request` | `not_found` (`404`) |
| Unknown `agent.model` | `invalid_request` | `model_not_found` |
| Nested `events` length not 1, or mixed flat+nested body | `invalid_request` | `validation_error` |
| Non-text input parts (for example `input_image`) | `not_implemented` | The part type |

The envelope is `{ "error": { "type", "code", "message" } }`. See
[API errors](api.md#errors).

## Events request and response

`POST /v1/agents/sessions/{id}/events` accepts:

```json
{
  "events": [
    {
      "type": "agent.session.input.message",
      "input": [
        {
          "role": "user",
          "content": [{ "type": "input_text", "text": "follow up" }]
        }
      ]
    }
  ]
}
```

That is what `client.beta.agents.sessions.events.create` sends. Cancel
and tool_result use the same `events` list with one object. The flat
body `{ "type": "agent.session.input.message", "text": "…" }` is the
other supported form.

SSE events use ApiPi public types (`agent.session.created`,
`agent.session.turn.output_text.done`, and the rest listed on
[API](api.md#events)). Each event has `seq`. Payload fields live under
`data`. Typed OpenAI stream objects that expect top-level `delta` or
`item_id` will not see those keys; parse `data` or use
`with_streaming_response`.

## How to verify

1. Install and serve the gateway ([Install](install.md)).
2. Run `examples/openai_sdk.py` as on [Using the API](using.md).
3. Send a follow-up with the nested `events` body (SDK
   `sessions.events.create`) or the flat curl example on that page.
4. Confirm unknown keys return `unknown_field` and `multi_agent` returns
   `not_implemented`.

Field-level HTTP reference remains [API](api.md). Computers and TTL are
on [environments](environments.md) and [concepts](concepts.md).
