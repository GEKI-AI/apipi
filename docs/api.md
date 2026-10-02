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
quota. See [auth](auth.md). Every query is tenant-scoped. When the
auth identity includes `user_id`, session reads and writes also
require that user. An id that belongs to another tenant or another
user returns `404`, not `403`.

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
| `GET` | `/v1/apipi/agents/{agent_id}/export` |

Fields: `id`, `name`, `model`, `instructions`, `idle_ttl`, `metadata`,
`tools` (function, mcp), `session_defaults`,
`created_at`, `updated_at`. Writing an unknown field such as `revision`
is `unknown_field`.
`idle_ttl` is an ApiPi extension: a duration such as `30m` or `1h`,
or `0` to turn the idle timer off. Omit it to keep the environment
default. See [config](config.md).

`session_defaults` stores the environment and vaults that later
sessions inherit. It has `environment` (the same object as session
create) and `vault_ids`. Create and update replace the whole object
when the field is present. `null` clears it. Skill, file, and vault
ids must already exist in the tenant. A missing id is `404`. Hosted-only
fields on a non-hosted type, too many skills or files, an unknown
image, or a size below that image's minimum are `400`, the same as
session create. Deleting a skill, file, or vault does not block the
delete. A later session that still inherits that id fails with `400`
and names the agent and the missing id.

Session create resolves each field as explicit session value, then
agent default, then the server default. `inherit_agent_defaults`
defaults to true. Set it to false to ignore `session_defaults` for
that session, including the sandbox aliases below. Maps merge by key
(`env`, and `packages` per ecosystem). Files merge by path. Skills and
vault ids are unions, agent first, with duplicates dropped. Type,
sandbox size, sandbox image, capability directories, setup commands,
and network replace as a whole. The agent's environment defaults apply
only when the session omits `environment` or uses the same type (after
the `hosted` alias). A different type keeps the session environment
and still applies sandbox size and image. The session row stores the merged environment. `environment.env`
values are stored on the agent row in plain text. Use `vault_ids` for
credentials.

`metadata["apipi.sandbox_image"]`
is the stock-SDK path for `session_defaults.environment.sandbox_image`.
Create and update copy it into
`session_defaults` and mirror the stored value back into metadata so
older clients still see the key. If both are set and they differ, the
write is `400`. A missing value keeps the gateway default. Worker
availability is not checked until a session is created. See
[environments](environments.md).

Rejected: `multi_agent`, `tool_search`, `programmatic_tool_calling`.

`GET /v1/apipi/agents/{agent_id}/export` downloads a template bundle for that
agent without storing a template. The zip is the same format as
[agent templates](agent-templates.md). Secrets and credential values
are not included.

## Templates

ApiPi-only routes live under `/v1/apipi/`. The old paths outside that prefix are gone and return `404` (see the changelog for the old-to-new mapping).

`POST /v1/apipi/auth/invalidate` drops cached auth identities for
the caller's tenant. The body filters are optional and combined with
AND: `{"key_id", "user_id"?, "org_id"?}`. An empty body means every
cached identity of the caller's tenant. The response is
`{"invalidated": <count>}`. See [auth](auth.md).

A template is a stored zip of one agent's configuration. It is
tenant-scoped. Creating an agent from a template always creates a new
agent. It does not update an existing agent, and deleting the template
does not change agents already created from it.

| Method | Path |
| --- | --- |
| `POST` | `/v1/apipi/templates` |
| `POST` | `/v1/apipi/templates/import` |
| `GET` | `/v1/apipi/templates` |
| `GET` | `/v1/apipi/templates/{template_id}` |
| `GET` | `/v1/apipi/templates/{template_id}/download` |
| `DELETE` | `/v1/apipi/templates/{template_id}` |
| `POST` | `/v1/apipi/templates/{template_id}/agents` |

`POST /v1/apipi/templates` takes `{"agent_id", "name"?, "description"?}` and
returns the template object. `POST /v1/apipi/templates/import` uploads a zip
as multipart field `bundle`, with optional form fields `name` and
`description`. Both return the template object: `id`, `name`,
`description`, `schema_version`, `visibility` (`tenant` only),
`created_by`, `size`, `sha256`, `requires`, `warnings`, `created_at`,
`updated_at`. `created_by` is the auth `user_id` when the plugin sets
one. It does not grant or deny access.

Download streams the zip when the artifact store is local. When the
store is S3, the response is `302` to a presigned GET. Another tenant's
id is `404`.

`POST /v1/apipi/templates/{template_id}/agents` takes `secrets`,
`credentials`, and `overrides` (`name`, `model`). It returns `agent`,
`skills`, `missing`, and `warnings`. A missing secret or credential
mapping still creates the agent and lists the name under
`missing.secrets` or `missing.credentials`. A supplied credential id
from another tenant is `400`. An unknown model is listed under
`missing.models` and the agent is still created. An unknown image or
an invalid size fails the create with `400` and stores no agent, skill,
or file. Upload of that same bundle succeeds with a warning.

The bundle layout, placeholders, and version rules are in
[agent templates](agent-templates.md).

A session may pass `agent_id` or an inline `agent`. You must provide
exactly one of those. Inline config is used for that session only. It
is not saved unless you `POST /v1/agents`. A live turn needs
`agent.model`. Session create with input and no model is `400` with
code `model_required`, before Pi starts. Creating or editing an agent
checks that id against the model list. An unknown id is `400` with
code `model_not_found`. A host that cannot list models is `400`
`model_host_unreachable`, or `401` `model_host_unauthorized`. A later
turn does not repeat that check. If the host then rejects the model,
the turn fails and the session returns to `idle`. The public code on
`agent.session.error` is the specific code (copied in `detail_code`, with `legacy_code` still `model_host_error`). Set `APIPI_ERROR_CODES=legacy` to keep `model_host_error` in `code` for one release. Failure modes are in
[configuration](config.md#failure-modes) and
[failure codes](errors.md).
Inline `model` and `instructions` are kept on the
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
`X-Content-Type-Options: nosniff`. `POST /v1/apipi/files/{id}/download`
returns a short-lived GET URL when the artifact store is S3.

## Uploads

S3-compatible object storage only (`APIPI_ARTIFACT_STORE=s3`). Local
store returns `400` with code `presign_unsupported`.

| Method | Path |
| --- | --- |
| `POST` | `/v1/apipi/uploads` |
| `POST` | `/v1/apipi/uploads/{upload_id}/complete` |
| `POST` | `/v1/apipi/files/{file_id}/download` |
| `POST` | `/v1/apipi/skills/{skill_id}/download` |
| `POST` | `/v1/apipi/sessions/{session_id}/artifacts/{artifact_id}/download` |

Create takes `purpose` (`file`, `attachment`, or `skill`), `filename`,
`bytes`, and optional `content_type`. `attachment` is the same store as
`file`. The response is a PUT URL and
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

When `APIPI_FORWARD_MODELS` is on (the default) and `APIPI_MODEL_LIST`
is `probe` or `turn`, this route proxies to `{OPENAI_BASE_URL}/models`
on the model host. The JSON body is the host's list, unchanged. Auth
is the usual bearer. The host call uses `OPENAI_API_KEY_OVERWRITE`
when that is set, otherwise the request bearer: the same key Pi uses.
When `APIPI_MODEL_LIST` is `off`, the route returns the static
`APIPI_MODELS` list and does not call the host. An empty static list
is `{"object": "list", "data": []}`.

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
object with `content` or `text`. A non-empty input starts the first turn. Non-stream create waits for
that turn. If it fails, the response is `502` with the turn error
`code` and `session_id` on the error object. `stream: true` returns SSE
as soon as the session row exists; turn events follow while Pi runs.
If that first turn fails, including when no worker can take it, the
stream includes `agent.session.error` with a `code` and then
`agent.session.failed`. The stream ends on `agent.session.failed`, so
a client does not wait for a later event.

Status: `idle | in_progress | requires_action | failed`. That is the turn, not the sandbox. An idle session can still have a live computer.

`required_actions`: `function_call`. The next turn rebuilds the computer when it needs one.

A hosted session (`openai_hosted`) has `environment.id`, `environment.status`, and `environment.sandbox`. `environment.status` is the OpenAI value: `provisioning` while the computer is starting, `connected` when Pi is ready, `disconnected` when it has not started or has stopped, `failed` when boot or setup failed. `environment.sandbox` is ApiPi detail: `state` (`none`, `starting`, `ready`, `stopped`, `failed`), `reason`, `since`, `image`, `image_version`, `size`, `cold_boots`, and `last_boot_ms`. It does not include a worker id. `none` sets `environment.sandbox` to null.

`GET /v1/agents/environments/{environment_id}` returns `id`, `type`, `status`, and `sandbox` for that session. Another tenant's id is `404`. There is no pause. A stop deletes the hosted workspace. Clients may label a stopped computer "paused" in the UI, but files do not survive.

Boot stays lazy until the first turn unless `APIPI_SANDBOX_EAGER_BOOT` is on, or the session or agent sets `metadata["apipi.sandbox_eager_boot"]`. When that is on, create starts the boot and the client can wait for `environment.connected` before sending input. A warm reuse emits no environment event. The current state is on GET.

When auth includes `user_id`, create stores it on the session and
returns it as `user_id`. List, get, update, delete, and later turns
then see only that user's sessions. Without `user_id`, `user_id` is
null and sessions stay visible to the whole tenant. When auth
includes `org_id`, create stores it and returns it as `org_id`.
`org_id` does not filter list or get. See [auth](auth.md).

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
reserved. The gateway interprets
`apipi.sandbox_image`,
`apipi.system_prompt`, `apipi.codemode`, `apipi.builtin_tools`, and
`apipi.idle_ttl`, and it rejects
`apipi.sandbox_size` and `apipi.thinking` with `400`. It
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
`content` has `input_text` and, for a vision model, `input_image`.
`input_image.image_url` must be a `data:` URL (`png`, `jpeg`, `webp`,
or `gif`). Remote `http` and `https` URLs are rejected. A model that
is not in the registry, or whose `input` does not include `image`,
returns `400` with code `unsupported_input`. Image bytes are stored as
Files API objects. The item keeps `file_id`, not the base64. Flat form: `type`
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
short preview, not as live deltas. In split mode the worker sends
text fragments as ephemeral messages over its socket (coalesced over
about 40ms), and the API fans them out over the event bus, so token
streaming works on any replica with no sticky routing; a delta that
arrives after its turn already committed `output_text.done` is
dropped. In combined serve, behind more than one
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
| `agent.session.turn.compaction.started` | Pi started compaction. Stored. `reason` when Pi sent one (`manual`, `threshold`, or `overflow`). |
| `agent.session.turn.compaction.completed` | Pi finished compaction. Stored. `reason`, `aborted`, `will_retry`, `tokens_before`, `tokens_after`, and a short `error` when present. The summary text is not stored. |
| `agent.session.turn.retrying` | Pi will retry the model call. Stored. `attempt`, `max_attempts`, `delay_ms`, `code`, `failure_source`, `upstream_status`. The raw error text is not stored. |
| `agent.session.turn.retry.completed` | That retry wait finished. Stored. `success`, `attempts`. A success does not end the turn. |
| `agent.session.environment.pending` | Hosted cold boot started. Hosted `data.sandbox` has `state`, `cold`, `cause`, `image`, and `size`. |
| `agent.session.environment.connected` | Computer ready. Hosted `data.sandbox` has `image`, `image_version`, `size`, `run_mode`, `boot_ms`, `lock_wait_ms`, and `setup_ms`. |
| `agent.session.environment.disconnected` | Computer gone. Hosted `data.sandbox` has `reason` and `live_ms`. |
| `agent.session.environment.failed` | Could not attach, or hosted setup failed. Hosted `data.sandbox.state` is `failed`. |

Item types: `message`, `function_call`, `mcp_call`,
`mcp_list_tools`, `command_execution`. Thinking is not an item. `GET /items` does not
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
with `reasoning.effort`. `metadata["apipi.thinking"]` is removed as client
input and is `400`. `none` means
off. See [Pi](config.md#pi).

Compaction events are optional. A Pi build that does not emit
`compaction_start` or `compaction_end` does not fail the turn. The
summary text is not a public event.

Retry events are optional in the same way. `agent.session.turn.retrying`
is one wait before the next model attempt. `attempt` is Pi's retry
number, starting at 1. `max_attempts` is `retry.maxRetries`, not the
total number of calls. `agent.session.turn.retry.completed` follows
when that wait ends. `success` false during a user cancel does not
fail the turn. The turn is classified only after the last attempt.
See [failure codes](errors.md).

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
| `GET` | `/v1/apipi/sessions/{session_id}/export` |

JSON of the transcript from the store: public events, turns, and items.
Same shapes as the list endpoints. Does not read Pi files. Wrong tenant
is `404`. A session export is enough to leave: the customer keeps the
thread if the gateway disappears.

## Usage

| Method | Path |
| --- | --- |
| `GET` | `/v1/apipi/usage` |

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
| `none` | No computer. Function tools and HTTP MCP only (anything else is `400`). |
| `self_hosted` | Not supported for now. Requests return `400` with type `not_implemented` (`environment type self_hosted is not supported`). It may come back later on worker protocol v2. Use `openai_hosted`. |

`environment.capability_directories`: paths on the computer that contain
`SKILL.md` trees. See [tools](tools.md).

On `openai_hosted` (and the `hosted` alias), create also accepts
`packages`, `setup_commands`, `env`, `files`, `skills`, and `network`.
`packages` is an object with optional `python`, `system`, and `npm`
lists of package names (pin versions when you need to, such as
`pandas==2.2.3`). Python packages install into `.venv` in the session
workspace. npm packages install under `.npm` there. Both directories
are put first on `PATH` for that session. On `microvm` those installs
use guest RAM because `/workspace` is a tmpfs. `packages.system` uses
`apk` or `apt-get` on isolation `none`. On `microvm` the guest root is
read-only, so `packages.system` returns `400` and tells you to bake
those packages into a guest image. `setup_commands` is an ordered list of
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
`none` return `400`.
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
body is still `unknown_field`. Set `environment.container_size`
(`small`, `medium`, `large`) or `environment.sandbox_size`. Agent
`session_defaults.environment.sandbox_size` is the default for later
sessions. The gateway default is
`APIPI_SANDBOX_DEFAULT_SIZE` (`S` unless you change it). The resolved
size is stored on the session `environment` and does not change if you
later PATCH metadata. Isolation `none` accepts the field and ignores
RAM and rootfs. Isolation `microvm` uses the size for guest RAM. The
guest image comes from `environment.sandbox_image`,
`session_defaults.environment.sandbox_image`, or
`metadata["apipi.sandbox_image"]`. When those are omitted, size `L`
selects `browser` and other sizes use the default image. Image
`browser` packs the built-in `browser` skill. The model uses bash and
`agent-browser`. It does not inject Playwright MCP. Install that
rootfs with
`apipi install --microvm --image browser`. Image `work` is the
business-document guest. It needs size `M` or larger. Install it with
`apipi install --microvm --image work`. A known image that no
worker has is `503` with code `image_unavailable`. See
[environments](environments.md).

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
That is a turn failure (`agent.session.turn.failed`), not
`agent.session.failed`. A non-stream create that fails the first turn
returns `502`. The body `code` is the
specific code (copied in `detail_code`, with `legacy_code` still `model_host_error` for upstream failures). The body also has `failure_source`, `upstream_status`,
`retryable`, and `upstream_attempts` when a count is known. A terminal session failure emits
`agent.session.error` and then `agent.session.failed`, and status
becomes `failed`. Codes, sources, and log levels are in
[failure codes](errors.md). Model-list outcomes are in
[configuration](config.md#failure-modes).

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
the local `.artifacts` tree, or an S3/botocore error such as
`AccessDenied`, `NoSuchBucket`, or a connection failure), the turn
fails with `agent.session.turn.failed` and `agent.session.error` with
code `artifact_store`. A missing object (`NoSuchKey`) is not that
error. If a Pi session cache is stored and reading it fails, the turn
fails the same way and does not continue without the cache. Session
create that cannot read a hosted file or skill returns `503` with
code `artifact_store`. A later turn that cannot read those bytes
fails the environment with the same code. An HTTP read, upload, or
download that hits the same store error also returns `503` with code
`artifact_store`, not `500`.
Settings and defaults are in [config](config.md).
