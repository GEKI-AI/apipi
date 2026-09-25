# API

Every public route lives under `/v1`. The beta header
`OpenAI-Beta: agents=v1` is accepted and ignored.

Unknown JSON keys return `invalid_request` with code `unknown_field`.
Known OpenAI fields we have not implemented return `not_implemented`.
They are not stored and they are not ignored. Extra JSON keys are
rejected because request bodies use strict models. The comparison
matrix is [OpenAI compatibility](openai-compatibility.md).

Auth is `Authorization: Bearer` on every request except `/health` and
`/metrics`. The gateway does not mint or store the auth bearer. A callback maps
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

Fields: `id`, `name`, `model`, `instructions`, `idle_ttl`, `metadata`,
`tools` (function, mcp HTTP, mcp stdio), `created_at`, `updated_at`.
`idle_ttl` is an ApiPi extension: a duration such as `30m` or `1h`,
or `0` to turn the idle timer off. Omit it to keep the environment
default. See [config](config.md).

Rejected: `multi_agent`, `tool_search`, `programmatic_tool_calling`.

A session may pass `agent_id` or an inline `agent`. You must provide
exactly one of those. Inline config is used for that session only. It
is not saved unless you `POST /v1/agents`. A live turn needs
`agent.model`. That id must exist on `OPENAI_BASE_URL`. Missing model
is `400` with code `model_required`. Unknown model is `400` with code
`model_not_found`. Inline `model` and `instructions` are kept on the
session for follow-up turns. Saved agents keep reading the agent row.
The gateway appends a platform prompt, then `agent.instructions` when
those are set. A system prompt may replace Pi's harness default first.
See [Concepts](concepts.md#agents) and [config](config.md#pi).

`vault_ids` on session create attaches vaults for HTTP MCP. The
gateway matches `mcp_server_url` and injects the bearer on the host
broker. Tokens are encrypted at rest. GET of a vault or credential
never returns the token.

## Vaults

| Method | Path |
| --- | --- |
| `POST` | `/v1/agents/vaults` |
| `GET` | `/v1/agents/vaults` |
| `GET` | `/v1/agents/vaults/{vault_id}` |
| `POST` | `/v1/agents/vaults/{vault_id}` |
| `DELETE` | `/v1/agents/vaults/{vault_id}` |
| `POST` | `/v1/agents/vaults/{vault_id}/credentials` |
| `GET` | `/v1/agents/vaults/{vault_id}/credentials` |
| `GET` | `/v1/agents/vaults/{vault_id}/credentials/{id}` |
| `POST` | `/v1/agents/vaults/{vault_id}/credentials/{id}` |
| `DELETE` | `/v1/agents/vaults/{vault_id}/credentials/{id}` |

Create a vault with `name` and `metadata`. Add a credential with
`auth.type` `static_bearer`, `mcp_server_url`, and `token`. The store
keeps the token as AES-256-GCM ciphertext (`APIPI_VAULT_MASTER_KEY`).
List and get omit `token`. `auth.type` `mcp_oauth` is
`not_implemented`. Every query is tenant-scoped. A vault from another
tenant is `404`.

## Files

| Method | Path |
| --- | --- |
| `POST` | `/v1/files` |
| `GET` | `/v1/files` |
| `GET` | `/v1/files/{file_id}` |
| `GET` | `/v1/files/{file_id}/content` |
| `DELETE` | `/v1/files/{file_id}` |

Upload is multipart form data with `file` and `purpose`. Accepted
purposes are `user_data` and `assistants`. Other purposes return
`not_implemented`. The object is `{ id, object: "file", bytes,
created_at, filename, purpose, status }`. `created_at` is a Unix
timestamp. Ids look like `file-` plus hex. Bytes live in the same
object store as artifacts (`APIPI_ARTIFACT_STORE`). Metadata is in
Postgres. The upload cap is `APIPI_MAX_FILE_BYTES` (default 50 MiB).
A larger body returns `413` with code `payload_too_large`. A file
from another tenant is `404`. Attach an uploaded file on session
create with `environment.files` `{ "type": "file_id", "file_id":
"…", "path": "/workspace/…" }`.

Browser and BFF uploads that must not proxy bytes through the gateway
use [presigned uploads](#uploads) instead of this multipart route.
`GET /v1/files/{id}/content` still streams through the gateway. The
response uses `Content-Disposition: attachment` with the stored file
name, including an RFC 5987 `filename*` when the name is not ASCII, and
`X-Content-Type-Options: nosniff`. `POST /v1/files/{id}/download`
returns a short-lived GET URL when the artifact store is S3.

## Uploads

S3-compatible object storage only (`APIPI_ARTIFACT_STORE=s3`). Local
store returns `400` with code `presign_unsupported`.

| Method | Path |
| --- | --- |
| `POST` | `/v1/uploads` |
| `POST` | `/v1/uploads/{upload_id}/complete` |
| `POST` | `/v1/files/{file_id}/download` |
| `POST` | `/v1/skills/{skill_id}/download` |
| `POST` | `/v1/agents/sessions/{session_id}/artifacts/{artifact_id}/download` |

Create takes `purpose` (`file`, `attachment`, or `skill`), `filename`,
`bytes`, and optional `content_type`. `attachment` is the same store as
`file` (chat attachments reuse Files). The response is a PUT URL and
headers. PUT the bytes to object storage, then complete. Complete
checks the object with `HeadObject`, enforces `APIPI_MAX_FILE_BYTES`,
and writes Files or Skills metadata. Complete before PUT is `400` with
code `upload_incomplete`. Wrong tenant is `404`. The Pi harness session
cache is not exposed this way.

A presigned GET forces a download. The URL sets
`Content-Disposition: attachment` to the original file name. A name that
is not plain ASCII also gets an RFC 5987 `filename*` parameter. The URL
sets `Content-Type` from the stored type. HTML, SVG, XML, and
JavaScript are signed as `application/octet-stream` so a browser does
not render them from the bucket domain. The same attachment header is
used when the gateway streams `/content`.

Do not put the ApiPi API key in the browser. Do not log the presigned
URL.

## Skills

| Method | Path |
| --- | --- |
| `POST` | `/v1/skills` |
| `GET` | `/v1/skills` |
| `GET` | `/v1/skills/{skill_id}` |
| `DELETE` | `/v1/skills/{skill_id}` |

Upload is multipart form data with field `files` (a zip). The zip must
contain exactly one `SKILL.md`. The object is `{ id, object: "skill",
name, bytes, created_at }`. Ids look like `skill-` plus hex. Bytes
live in the shared object store. The upload cap is
`APIPI_MAX_FILE_BYTES`. A skill from another tenant is `404`. Attach
on session create with `environment.skills` `{ "type":
"skill_reference", "skill_id": "…" }`. ApiPi unpacks under
`.agents/skills/`. At most 32 skills per create. There are no version
endpoints.

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

## Chat

GEKI-native chat sessions. Not OpenAI `/v1/chat/completions`. The
store is the same session and event log as Agents. Clients never set
or see `environment`. The gateway stores `environment.type=none` and
`metadata.apipi.session_kind=chat` so placement uses chat workers.
Fleet layout and placement footguns are in [chat fleets](chat.md).

To attach a computer later, create a new Agents session. Chat sessions
do not upgrade in place.

| Method | Path |
| --- | --- |
| `POST` | `/v1/chat/sessions` |
| `GET` | `/v1/chat/sessions` |
| `GET` | `/v1/chat/sessions/{session_id}` |
| `POST` | `/v1/chat/sessions/{session_id}` |
| `DELETE` | `/v1/chat/sessions/{session_id}` |
| `POST` | `/v1/chat/sessions/{session_id}/events` |
| `GET` | `/v1/chat/sessions/{session_id}/events` |
| `GET` | `/v1/chat/sessions/{session_id}/turns` |
| `GET` | `/v1/chat/sessions/{session_id}/turns/{turn_id}` |
| `GET` | `/v1/chat/sessions/{session_id}/items` |
| `GET` | `/v1/chat/sessions/{session_id}/export` |

Create accepts `agent` or `agent_id`, `input`, `metadata`, `vault_ids`,
and `stream`. `stream: true` returns SSE as soon as the session exists.
An `environment` field is `400` with code
`unknown_field`. List returns only chat sessions. An Agents session id
on a chat path is `404`. Event types match Agents so one frontend can
read both.

Chat tools are an allowlist: function tools and HTTP MCP. Stdio MCP,
Playwright auto-inject, workspace skills, and computer environments
are rejected. A disallowed tool is `400` with code `chat_tool`. Saved
agents used as chat profiles should set `metadata.apipi.session_kind`
to `chat`; create and update then apply the same allowlist.

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
object with `content` or `text`. A non-empty input starts the first turn. Non-stream create waits for
that turn. If it fails, the response is `502` with the turn error
`code` and `session_id` on the error object. `stream: true` returns SSE
as soon as the session row exists; turn events follow while Pi runs.
If that first turn fails, including when no worker can take it, the
stream includes `agent.session.error` with a `code` and then
`agent.session.failed`. The stream ends on `agent.session.failed`, so
a client does not wait for a later event.

Status: `idle | in_progress | requires_action | failed`.

`required_actions`: `function_call`, `environment_connection`.

`POST /v1/agents/sessions/{session_id}` updates `metadata` only.
`DELETE` stops the live guest on the worker that holds the lease,
removes stored artifact bytes, then removes the session row. It
returns `{"id": "…", "deleted": true}`. The guest does not stay up
until the worker drains.

Session create may set `idle_ttl` to the same duration. That value
wins over the agent field. Stock clients can set
`metadata["apipi.idle_ttl"]` instead of the top-level field. See
[config](config.md).

`metadata` is a JSON object. Keys that start with `apipi.` are
reserved. The gateway interprets `apipi.sandbox_size`,
`apipi.session_kind`, `apipi.thinking`, `apipi.system_prompt`,
`apipi.idle_ttl`, `apipi.title`, and `apipi.title_status`. It
stores `apipi.actor_type`, `apipi.schedule_id`, and `apipi.source`
and does not branch on them. There is no top-level `actor_type`
field. See [reserved metadata](extending.md#reserved-metadata).

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
the session is `in_progress` cancels that turn (or fails it if the
process no longer owns it) and starts a new turn, so a hung Pi cannot
block the next command. `GET` of a session that is `in_progress` with
no live turn on this process does the same fail-and-idle recovery.
A message while the session is `requires_action` is rejected; send a
tool result instead.

Tool result: `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` (on success) or `error` (on
failure), either nested in `events` or flat.

Cancel: `agent.session.input.cancel` on a session in `in_progress`.
The gateway persists `agent.session.turn.cancelled` then
`agent.session.idle`.

`GET` returns `{"data": […]}`. `GET ?stream=true` is SSE. The stream
stays open across `idle` and ends after `agent.session.failed`. It
sends SSE comment keepalives (`: ping`)
without a blank line, so clients that parse every dispatched event as
JSON do not see an empty payload. Reconnect and replay from the store
with `after_seq`. Stored public events are written before SSE.
`output_text.delta` is live SSE only and is not stored; reconnect and
export skip those fragments. Full assistant text is on
`output_text.done` and the assistant item. Thinking is stored as a
short preview, not as live deltas. Behind more than one
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
| `agent.session.turn.thinking.started` | Thinking block started. Stored. `item_id`, `content_index`. |
| `agent.session.turn.thinking.completed` | Thinking block finished. Stored. Preview only, not the full text. |
| `agent.session.turn.thinking.summary.completed` | Short summary of that block. Stored. `item_id`, `summary`, `summary_status=done`. |
| `agent.session.turn.thinking.summary.failed` | Summary was not produced. Stored. `item_id`, `summary_status=failed`. No summary text. |
| `agent.session.turn.compaction.started` | Pi started compaction. Stored. `reason` when Pi sent one (`manual`, `threshold`, or `overflow`). |
| `agent.session.turn.compaction.completed` | Pi finished compaction. Stored. `reason`, `aborted`, `will_retry`, `tokens_before`, `tokens_after`, and a short `error` when present. The summary text is not stored. |
| `agent.session.title.updated` | `metadata["apipi.title"]` was set or the title job failed. Stored. |
| `agent.session.environment.pending` | Waiting for a computer |
| `agent.session.environment.connected` | Computer ready |
| `agent.session.environment.disconnected` | Computer gone |
| `agent.session.environment.failed` | Could not attach, or hosted setup failed |

Item types: `message`, `function_call`, `mcp_call`,
`command_execution`. Thinking is not an item. `GET /items` does not
list it.

`agent.session.turn.thinking.completed` carries `item_id`,
`content_index`, `duration_ms`, `reasoning_tokens`, `preview`, and
`preview_truncated`. `preview` is the first 100 Unicode code points.
`preview_truncated` is true when the block was longer. `duration_ms`
is null when the start event was missed. `reasoning_tokens` is Pi's
reasoning count at the end of the block, or null when the host did
not report one. The full thinking text is not on these events, not
in items, and not in logs. There is no admin API that returns it.
Pi's session cache may still hold the full text. That cache is not
the public transcript. Thinking deltas are not sent to clients.
Enable thinking with `APIPI_PI_THINKING`, or override it per session
with `metadata["apipi.thinking"]`. See [Pi](config.md#pi).

Compaction events are optional. A Pi build that does not emit
`compaction_start` or `compaction_end` does not fail the turn. The
summary text is not a public event.

A thinking summary is optional and arrives later on
`agent.session.turn.thinking.summary.completed`. It does not replace
the preview. Clients can show `summary` when that event has arrived,
and the preview otherwise. A failed summary does not fail the turn.
The platform flag and the auth callback must both allow it. See
[configuration](config.md) and [auth](auth.md).

`metadata["apipi.title"]` is the automatic session title when that
feature is on. `metadata["apipi.title_status"]` is `pending`, `done`,
or `failed`. A metadata update that omits those keys keeps the stored
values. The gateway does not replace an existing title.

## Turns, items, artifacts

| Method | Path |
| --- | --- |
| `GET` | `/v1/agents/sessions/{session_id}/turns` |
| `GET` | `/v1/agents/sessions/{session_id}/turns/{turn_id}` |
| `GET` | `/v1/agents/sessions/{session_id}/items` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts` |
| `GET` | `/v1/agents/sessions/{session_id}/artifacts/{id}/content` |
| `DELETE` | `/v1/agents/sessions/{session_id}/artifacts/{id}` |

When a turn completes, files under `outputs/` on the computer are
copied into the artifact store. Copies are immutable and include
`turn_id`. A later turn that writes the same path publishes another
artifact. Rows already stored with a path under `artifacts/` stay
readable; new publishes use `outputs/`. `GET` content works as soon as the turn has
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
`packages`, `setup_commands`, `env`, `files`, `skills`, and `network`.
`packages` is an object with optional `python`, `system`, and `npm`
lists of package names (pin versions when you need to, such as
`pandas==2.2.3`). `setup_commands` is an ordered list of
`{ "command": "…", "cwd": "…" }` objects. `cwd` is optional and
defaults to the session workspace. `env` is an object of string
environment variables for that session. `files` entries are
`{ "type": "inline", "path": "/workspace/…", "data": "<base64>" }`
or `{ "type": "file_id", "file_id": "file-…", "path": "/workspace/…" }`.
`file_id` must be a Files API object owned by this tenant. Other
`files` types return `not_implemented`. At most 50 files per create.
`skills` entries are `{ "type": "skill_reference", "skill_id": "…" }`.
Other skill types return `not_implemented`. At most 32 skills per
create. A missing or foreign `skill_id` is `404`.
`network` is `{ "access": "enabled"|"disabled"|"restricted",
"allowed_domains": ["api.example.com"] }`. `allowed_domains` is
required for `restricted` (1–100 exact hostnames, no wildcards,
schemes, paths, or ports) and is otherwise rejected. Files are
written first, then packages install, then setup commands run, before
the first agent turn. A nonzero install or setup exit emits
`agent.session.environment.failed` and fails the session; Pi does not
start. `packages`, `setup_commands`, `env`, `files`, and `skills` on
`none` or `self_hosted` return `400`. `network` on `self_hosted`
returns `400`.
On environment type `none` it is ignored. Isolation `none` cannot
enforce `disabled` or `restricted` and fails the environment instead.
Session `network` cannot open hosts that `[sandbox.network]` forbids.
`environment_template_id` and `plugins` return `400` with
type `not_implemented`. Reserved `env` names (`PATH`, `HOME`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `DATABASE_URL`,
`PI_CODING_AGENT_DIR`, and `APIPI_` / `CODEX_` / `PI_` prefixes)
return `400`. Decoded files must fit
`APIPI_MAX_WORKSPACE_BYTES`. A missing or foreign `file_id` is `404`.

`environment.sandbox_size` is an ApiPi extension: `S`, `M`, or `L`.
Unknown values return `400`. A top-level `sandbox_size` on the session
body is still `unknown_field`. Stock OpenAI clients can set
`metadata["apipi.sandbox_size"]` instead. Agent metadata with that key
is a default for later sessions. The gateway default is
`APIPI_SANDBOX_DEFAULT_SIZE` (`S` unless you change it). The resolved
size is stored on the session `environment` and does not change if you
later PATCH metadata. Isolation `none` accepts the field and ignores
RAM and rootfs. Isolation `microvm` uses it for guest RAM and image:
`S`/`M` boot the default rootfs, `L` boots the browser rootfs. `L`
without that rootfs fails clearly (combined create returns `400`;
API-only fails when the worker spawns). On `microvm`, `L` also injects
Playwright MCP unless the agent already has it or auto-inject is off.
See [environments](environments.md).

## Compatibility

The official client surface, backend differences, and error matrix are
on [OpenAI compatibility](openai-compatibility.md). The OpenAI Python
example is `examples/sessions/openai_sdk.py`. Create-and-stream steps are in
[Using the API](using.md).

## Errors

```json
{ "error": { "type": "not_implemented", "code": "...", "message": "...", "session_id": "..." } }
```

`session_id` is set when create already stored a session and the first
turn failed. The session stays `idle` so a follow-up message works.

Missing or invalid bearer is `401` with code `unauthorized`. An auth
plugin may return `429` with a plugin `code` such as `rate_limited` or
`quota`. A new turn that would pass `APIPI_MAX_SESSIONS` live Pi
processes returns `429` with code `capacity`. A known
`sandbox_image` that no live worker has returns `503` with code
`image_unavailable`. Workers that have the image but are full still
return `429` `capacity`. An unknown image id is `400`. A tenant that would pass
`APIPI_MAX_SESSIONS_PER_TENANT` returns `429` with code
`capacity_tenant`. A request body larger than
`APIPI_MAX_REQUEST_BYTES` returns `413` with code `payload_too_large`.
An `openai_hosted` directory over `APIPI_MAX_WORKSPACE_BYTES` emits
`agent.session.error` with code `workspace_too_large`. Publishing
artifacts that would pass `APIPI_MAX_ARTIFACT_BYTES` emits
`agent.session.error` with code `artifact_too_large`. If the artifact
store cannot be written (`OSError`, including a permission error on
the local `.artifacts` tree), the turn fails with
`agent.session.turn.failed` and `agent.session.error` with code
`artifact_store`. Settings and defaults are in [config](config.md).
