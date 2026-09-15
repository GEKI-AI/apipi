# API

Every public route lives under `/v1`. Official OpenAI clients work for
the subset we implement. The beta header `OpenAI-Beta: agents=v1` is
accepted and ignored.

Unknown fields and unimplemented features return an error
(`invalid_request` or `not_implemented`). They are not stored and they
are not ignored. Extra JSON keys are rejected because request bodies
use strict models.

Auth is `Authorization: Bearer` on every request except `/health` and
`/metrics`. The gateway does not mint or store keys. A callback maps
the bearer to `key_id` and `tenant_id`, or rejects with a status,
`code`, and `message`. Invalid keys are `401` with code
`unauthorized`. An auth plugin may return `429` for a rate limit or
quota. See [auth](auth.md). Every query is tenant-scoped. An id that
belongs to another tenant returns `404`, not `403`.

Clients send a bearer and talk to `/v1`.

## Agents

An agent is saved config, not a running process. A new install has no
agents until you create one.

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
is not saved unless you `POST /v1/agents`. A live turn needs
`agent.model`. That id must exist on `OPENAI_BASE_URL`. Missing model
is `400` with code `model_required`. Unknown model is `400` with code
`model_not_found`. Inline `model` and `instructions` are kept on the
session for follow-up turns. Saved agents keep reading the agent row.
When instructions are set, the gateway appends them to Pi's system
prompt. Empty or omitted instructions leave Pi's default prompt
unchanged.

## Models

| Method | Path |
| --- | --- |
| `GET` | `/v1/models` |

When `APIPI_FORWARD_MODELS` is on (the default), this route proxies to
`{OPENAI_BASE_URL}/models` on the model host. The JSON body is the
host's list, unchanged. Auth is the usual bearer. The host call uses
`OPENAI_API_KEY_OVERWRITE` when that is set, otherwise the request
bearer: the same key Pi uses.

A host `401` or `403` is `401` with code `model_host_unauthorized`. If
the host is unreachable, the response is `400` with code
`model_host_unreachable`. When `APIPI_FORWARD_MODELS` is off, the
route returns `400` with type `not_implemented` and code
`forward_models`.

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

`POST` accepts two bodies with the same meaning. The OpenAI Agents
shape is `{"events":[{...}]}` with exactly one event (what official
SDK helpers send). The flat shape is `{type, text|content, …}`. Send
one shape or the other, not both.

A message event starts a turn. Nested form: `type`
`agent.session.input.message` and `input` with a `user` message whose
`content` has `input_text`. Flat form: `type`
`agent.session.input.message` and `content` or `text`. Follow-up
messages work the same way after the session is idle. A message while
the session is `requires_action` is rejected; send a tool result
instead.

Tool result: `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` (on success) or `error` (on
failure), either nested in `events` or flat.

Cancel: `agent.session.input.cancel` on a session in `in_progress`.
The gateway persists `agent.session.turn.cancelled` then
`agent.session.idle`.

`GET` returns `{"data": […]}`. `GET ?stream=true` is SSE. The stream
stays open across `idle` and sends SSE comment keepalives (`: ping`)
without a blank line, so clients that parse every dispatched event as
JSON do not see an empty payload. Reconnect and replay from the store
with `after_seq`. Stored public events are written before SSE.
`output_text.delta` is live SSE only and is not stored; reconnect and
export skip those fragments. Full assistant text is on
`output_text.done` and the assistant item. Behind more than one
gateway process, the stream and the next turn must hit the node that
owns Pi. See [multiple nodes](scale.md).

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
| `agent.session.turn.failed` | Failed (including a model host error) |
| `agent.session.turn.cancelled` | Cancelled |
| `agent.session.turn.output_text.delta` | Assistant text fragment (live SSE only; not stored) |
| `agent.session.turn.output_text.done` | Text finished |
| `agent.session.turn.item.added` | New item |
| `agent.session.turn.item.done` | Item finished |
| `agent.session.environment.pending` | Waiting for a computer |
| `agent.session.environment.connected` | Computer ready |
| `agent.session.environment.disconnected` | Computer gone |
| `agent.session.environment.failed` | Could not attach, or hosted setup failed |

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
computer are copied into the artifact store. Copies are immutable and
include `turn_id`. A later turn that writes the same path publishes
another artifact. `GET` content works as soon as the turn has
completed, even if Pi is still alive. Harvest on Pi stop is a safety
net for files written after the last completed turn. `410` if nothing
was published. `DELETE` removes the metadata and the stored bytes. The
live file on the computer stays. Local disk is the default store. S3
is optional. See [run modes](run-modes.md#storage) and
[config](config.md).

`GET` turn may include `usage` (prompt, completion, cache read/write,
total). Tokens only. See [usage](usage.md).

## Export

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents/sessions/{session_id}/export` |

JSON of the transcript from the store: public events, turns, and items.
Same shapes as the list endpoints. Does not read Pi files. Wrong tenant
is `404`. A session export is enough to leave: the customer keeps the
thread if the gateway disappears.

## Usage

| Method | Path |
| --- | --- |
| `GET` | `/v1/usage` |

Tenant-scoped totals from hot usage data (turn log and/or daily
rollups). Filter by exactly one of `session_id`, `turn_id`, or `day`.
Tokens and turn counts, not USD. See [usage](usage.md).

## Request ids

Every public request except `/health` has an id. The gateway echoes
`x-request-id`. It generates a UUID if that header is missing. It
honors `X-Client-Request-Id` when present (ASCII, at most 512
characters). That client value becomes the request id. When
`APIPI_INSTANCE_ID` is set, responses also include `X-ApiPi-Instance`.
After a successful bearer, responses include `X-Tenant-Id` (auth
`tenant_id`) and `X-User-Id` (auth `key_id`). Those request headers are
not used for auth. When a trace is known (`traceparent`, or an active
OpenTelemetry span), responses include `X-Trace-Id`. `/health` omits
these. See [multiple nodes](scale.md).

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

On `openai_hosted` (and the `hosted` alias), create also accepts
`packages` and `setup_commands`. `packages` is an object with optional
`python`, `system`, and `npm` lists of package names (pin versions when
you need to, such as `pandas==2.2.3`). `setup_commands` is an ordered
list of `{ "command": "…", "cwd": "…" }` objects. `cwd` is optional and
defaults to the session workspace. Packages are installed first, then
setup commands run, before the first agent turn. A nonzero install or
setup exit emits `agent.session.environment.failed` and fails the
session; Pi does not start. Those fields on `none` or `self_hosted`
return `400`. `files`, `env`, `network`, `environment_template_id`,
`skills`, and `plugins` return `400` with type `not_implemented`.

See [environments](environments.md).

## Compatibility

Official clients work for the subset we implement. The OpenAI Python
client example is `examples/openai_sdk.py`. The same create-and-stream
steps are in [Using the API](using.md).

| Surface | Status |
| --- | --- |
| Agents CRUD | yes (subset of fields) |
| `GET /v1/models` | yes (proxy to the model host; off with `APIPI_FORWARD_MODELS`) |
| Sessions, stream, follow-up input | yes |
| `environment.openai_hosted` | yes (local sandbox) |
| `environment.hosted` | yes (alias of `openai_hosted`) |
| `environment.none` | yes |
| `environment.self_hosted` | yes (our protocol) |
| Function tools | yes |
| MCP | yes |
| Skills (`capability_directories`, `SKILL.md`) | yes |
| `environment.packages`, `setup_commands` | yes (`openai_hosted` only) |
| `environment.files`, `env`, `network` | no |
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

Missing or invalid bearer is `401` with code `unauthorized`. An auth
plugin may return `429` with a plugin `code` such as `rate_limited` or
`quota`. A new turn that would pass `APIPI_MAX_SESSIONS` live Pi
processes returns `429` with code `capacity`. A tenant that would pass
`APIPI_MAX_SESSIONS_PER_TENANT` returns `429` with code
`capacity_tenant`. A request body larger than
`APIPI_MAX_REQUEST_BYTES` returns `413` with code `payload_too_large`.
An `openai_hosted` directory over `APIPI_MAX_WORKSPACE_BYTES` emits
`agent.session.error` with code `workspace_too_large`. Publishing
artifacts that would pass `APIPI_MAX_ARTIFACT_BYTES` emits
`agent.session.error` with code `artifact_too_large`. Settings and
defaults are in [config](config.md).
