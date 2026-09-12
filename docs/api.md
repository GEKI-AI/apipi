# API

OpenAI Agents API subset. Prefix `/v1`. Beta header `OpenAI-Beta: agents=v1`
is accepted and ignored.

Unknown fields and unimplemented features return an error
(`invalid_request` or `not_implemented`). They are not stored and ignored.

Auth: `Authorization: Bearer`. We do not mint or store keys. A callback
maps the bearer to `key_id` and `tenant_id` ([auth](auth.md)).
Tenant-scoped. Wrong-tenant IDs are `404`.

The example UI uses a demo cookie on `/_example/` only.

## Agents

Saved config, not a running process.

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

A session may pass `agent_id` or an inline `agent`. Inline config is not
saved unless you `POST /v1/agents`. There is no built-in agent on a
fresh install.

## Sessions

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents/sessions` |
| `GET` | `/v1/agents/sessions` |
| `GET` | `/v1/agents/sessions/{session_id}` |
| `POST` | `/v1/agents/sessions/{session_id}` |
| `DELETE` | `/v1/agents/sessions/{session_id}` |

Create: `agent` or `agent_id`, `environment` (including
`capability_directories`), `input`, `metadata`, `stream`.

Status: `idle | in_progress | requires_action | failed`.

`required_actions`: `function_call`, `environment_connection`.

## Events

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents/sessions/{session_id}/events` |
| `GET` | `/v1/agents/sessions/{session_id}/events` |

`POST` body `agent.session.input.message` starts a turn, or steers a
running one.

Tool result: `agent.session.input.tool_result` with `turn_id`, `call_id`,
`success`, `output` or `error`.

`GET ?stream=true` is SSE. Stays open across `idle`. Reconnect and replay
from the store.

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
| `agent.session.turn.completed` | Done; may include `usage` |
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

Item types: `message`, `function_call`, `mcp_call`, `command_execution`.

## Turns, items, artifacts

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents/sessions/{session_id}/turns` |
| `GET` | `/v1/agents/sessions/{session_id}/turns/{turn_id}` |
| `GET` | `/v1/agents/sessions/{session_id}/items` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts/{id}/content` |
| `DELETE` | `/v1/agents/sessions/{session_id}/artifacts/{id}` |

Artifact bytes live on the sandbox. Content is proxied while it is
connected. `410` if it is gone.

## Environments

`environment.type` on create:

| Type | Behaviour |
| --- | --- |
| `openai_hosted` | **Default.** Session directory next to Pi. Not OpenAI's cloud. |
| `none` | No computer. MCP and chat only. |
| `self_hosted` | Wait for an external runner. |

`environment.capability_directories`: paths on the computer that contain
`SKILL.md` trees. See [tools](tools.md).

See [environments](environments.md).

## Compatibility

| Surface | Status |
| --- | --- |
| Agents CRUD | yes (subset of fields) |
| Sessions, stream, follow-up input | yes |
| `environment.openai_hosted` | yes (local sandbox) |
| `environment.none` | yes |
| `environment.self_hosted` | yes (our protocol) |
| Function tools | yes |
| MCP | yes |
| Skills (`capability_directories`, `SKILL.md`) | yes |
| Artifacts | yes |
| `web_search` first-party | no (MCP; example: Tavily) |
| Browser | no first-party (MCP; example: Playwright) |
| `/v1/skills` hosted store | no (files on the computer) |
| Vaults, multi-agent, tool search | no |
| ChatKit | no |

## Errors

```json
{ "error": { "type": "not_implemented", "code": "...", "message": "..." } }
```
