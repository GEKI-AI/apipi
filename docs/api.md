# API

ApiPi is a drop-in OpenAI Agents API. Every public route lives under
`/v1`. Official OpenAI clients work for the subset we implement. The
beta header `OpenAI-Beta: agents=v1` is accepted and ignored.

Unknown fields and unimplemented features return an error
(`invalid_request` or `not_implemented`). They are not stored and they
are not ignored. Extra JSON keys are rejected because request bodies
use strict models.

Auth is `Authorization: Bearer` on every request except `/health` and
`/metrics`. The gateway does not mint or store keys. A callback maps
the bearer to `key_id` and `tenant_id`. See [auth](auth.md). Every
query is tenant-scoped. An id that belongs to another tenant returns
`404`, not `403`.

There is no first-party chat UI. Clients send a bearer and talk to
`/v1`.

## Agents

An agent is saved config, not a running process. There is no built-in
agent on a fresh install.

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents` |
| `GET` | `/v1/agents` |
| `GET` | `/v1/agents/{agent_id}` |
| `POST` | `/v1/agents/{agent_id}` |
| `DELETE` | `/v1/agents/{agent_id}` |

Fields: `id`, `name`, `model`, `instructions`, `metadata`, `tools`
(function, mcp HTTP, mcp stdio), `created_at`, `updated_at`.

Rejected: `multi_agent`, `tool_search`, `programmatic_tool_calling`.

A session may pass `agent_id` or an inline `agent`. You must provide
exactly one of those. Inline config is used for that session only. It
is not saved unless you `POST /v1/agents`.

## Sessions

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents/sessions` |
| `GET` | `/v1/agents/sessions` |
| `GET` | `/v1/agents/sessions/{session_id}` |
| `POST` | `/v1/agents/sessions/{session_id}` |
| `DELETE` | `/v1/agents/sessions/{session_id}` |

Create accepts `agent` or `agent_id`, `environment` (including
`capability_directories`), `input`, `metadata`, and `stream`. If
`environment` is omitted, the type is `openai_hosted`: a local session
directory next to Pi, not OpenAI's cloud. `hosted` is an alias for
that same directory; the session stores and returns `openai_hosted`. `input` may be a string or an
object with `content` or `text`. A non-empty input starts the first
turn before the create response returns. `stream: true` returns SSE
instead of the session JSON.

Status: `idle | in_progress | requires_action | failed`.

`required_actions`: `function_call`, `environment_connection`.

`POST /v1/agents/sessions/{session_id}` updates `metadata` only.
`DELETE` removes the session for that tenant and returns
`{"id": "…", "deleted": true}`.

## Events

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents/sessions/{session_id}/events` |
| `GET` | `/v1/agents/sessions/{session_id}/events` |

`POST` with `type` `agent.session.input.message` starts a turn. Use
`content` or `text` for the user text. Follow-up messages work the same
way after the session is idle. A message while the session is
`requires_action` is rejected; send a tool result instead.

Tool result: `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` (on success) or `error` (on
failure).

Cancel: `agent.session.input.cancel` on a session in `in_progress`.
The gateway persists `agent.session.turn.cancelled` then
`agent.session.idle`.

`GET` returns `{"data": […]}`. `GET ?stream=true` is SSE. The stream
stays open across `idle` and sends `: ping` keepalives. Reconnect and
replay from the store with `after_seq`. The public event is written to
Postgres before it is published on SSE.

Only these event types are public. Anything else from Pi is an internal
log line.

| Type | Meaning |
| --- | --- |
| `agent.session.created` | Session exists |
| `agent.session.in_progress` | Turn running |
| `agent.session.idle` | No turn |
| `agent.session.requires_action` | Waiting on tool or environment |
| `agent.session.failed` | Terminal failure |
| `agent.session.error` | Error |
| `agent.session.turn.created` | Turn id |
| `agent.session.turn.in_progress` | Work started |
| `agent.session.turn.completed` | Done; may include `usage` (tokens only) |
| `agent.session.turn.failed` | Failed |
| `agent.session.turn.cancelled` | Cancelled |
| `agent.session.turn.output_text.delta` | Assistant text |
| `agent.session.turn.output_text.done` | Text finished |
| `agent.session.turn.item.added` | New item |
| `agent.session.turn.item.done` | Item finished |
| `agent.session.environment.pending` | Waiting for a computer |
| `agent.session.environment.connected` | Computer ready |
| `agent.session.environment.disconnected` | Computer gone |
| `agent.session.environment.failed` | Could not attach |

Item types: `message`, `function_call`, `mcp_call`,
`command_execution`.

## Turns, items, artifacts

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents/sessions/{session_id}/turns` |
| `GET` | `/v1/agents/sessions/{session_id}/turns/{turn_id}` |
| `GET` | `/v1/agents/sessions/{session_id}/items` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts/{id}/content` |
| `DELETE` | `/v1/agents/sessions/{session_id}/artifacts/{id}` |

When a turn completes, files under `artifacts/` and `outputs/` on the
computer are copied into the host store. Copies are immutable and
include `turn_id`. A later turn that writes the same path publishes
another artifact. `GET` content works as soon as the turn has
completed, even if Pi is still alive. Harvest on Pi stop is a safety
net for files written after the last completed turn. `410` if nothing
was published. `DELETE` removes the metadata and the stored file. The
live file on the computer stays. See [run modes](run-modes.md#storage).

`GET` turn may include `usage` (prompt, completion, cache read/write,
total). Tokens only. See [usage](usage.md).

## Export

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents/sessions/{session_id}/export` |

JSON of the transcript from Postgres: public events, turns, and items.
Same shapes as the list endpoints. Does not read Pi files. Wrong tenant
is `404`. A session export is enough to leave: the customer keeps the
thread if the gateway disappears.

## Usage

| Method | Path |
| --- | --- |
| `GET` | `/v1/usage` |

Tenant-scoped totals from the turn log. Filter by exactly one of
`session_id`, `turn_id`, or `day`. Tokens and turn counts, not USD.
See [usage](usage.md).

## Request ids

Every public request except `/health` has an id. The gateway echoes
`x-request-id`. It generates a UUID if that header is missing. It
honors `X-Client-Request-Id` when present (ASCII, at most 512
characters). That client value becomes the request id.

## Environments

`environment.type` on create:

| Type | Behaviour |
| --- | --- |
| `openai_hosted` | **Default.** Session directory next to Pi. Not OpenAI's cloud. |
| `hosted` | Alias for `openai_hosted`. Stored and returned as `openai_hosted`. |
| `none` | No computer. MCP and chat only. |
| `self_hosted` | Wait for an external runner. Create returns `environment_id` and a one-time `key`. Runner WebSocket: `/v1/environments/{environment_id}`. |

`environment.capability_directories`: paths on the computer that contain
`SKILL.md` trees. See [tools](tools.md).

See [environments](environments.md).

## Compatibility

Official clients work for the subset we implement. The OpenAI Python
client example is `examples/openai_sdk.py`. The same create-and-stream
steps are in [Using the API](using.md).

| Surface | Status |
| --- | --- |
| Agents CRUD | yes (subset of fields) |
| Sessions, stream, follow-up input | yes |
| `environment.openai_hosted` | yes (local sandbox) |
| `environment.hosted` | yes (alias of `openai_hosted`) |
| `environment.none` | yes |
| `environment.self_hosted` | yes (our protocol) |
| Function tools | yes |
| MCP | yes |
| Skills (`capability_directories`, `SKILL.md`) | yes |
| Artifacts | yes |
| Usage tokens on turns | yes |
| Session export | yes |
| `/v1/chat/completions` | no |
| `web_search` first-party | no (MCP; example: Tavily) |
| Browser | no first-party (MCP; example: Playwright) |
| `/v1/skills` hosted store | no (files on the computer) |
| Vaults, multi-agent, tool search | no |
| ChatKit | no |

## Errors

```json
{ "error": { "type": "not_implemented", "code": "...", "message": "..." } }
```

A new turn that would pass `APIPI_MAX_SESSIONS` live Pi processes
returns `429` with code `capacity`. A tenant that would pass
`APIPI_MAX_SESSIONS_PER_TENANT` returns `429` with code
`capacity_tenant`. A request body larger than
`APIPI_MAX_REQUEST_BYTES` returns `413` with code `payload_too_large`.
An `openai_hosted` directory over `APIPI_MAX_WORKSPACE_BYTES` emits
`agent.session.error` with code `workspace_too_large`. Publishing
artifacts that would pass `APIPI_MAX_ARTIFACT_BYTES` emits
`agent.session.error` with code `artifact_too_large`. Settings and
defaults are in [config](config.md).
