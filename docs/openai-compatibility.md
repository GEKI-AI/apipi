# OpenAI compatibility

ApiPi stays compatible with the OpenAI Agents API. Where a shape
exists upstream, this API uses it. Where that is impossible, the
extra behavior is under `/v1/apipi/`. See
[ADR 0014](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0014-openai-compat.md).
The decision file is not on this site.

Some OpenAI details are not verified against the full API reference:
the exact `GET /v1/agents/environments/{id}` object beyond `status`,
whether the session object itself carries `environment.status`, how
official SDKs parse unknown event types, and the full list of
top-level event fields. Unknown response fields are tolerated by the
SDKs we have tried. Do not treat unmarked rows below as a promise of
byte-for-byte schema parity.

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
follow-up, cancel, and delete. `examples/sessions/openai_sdk.py` and
[Using the API](using.md) walk through that flow.

You can:

- Create, list, get, update, and delete agents. Fields are `name`,
  `model`, `instructions`, `metadata`, and `tools` (function, MCP).
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
  or `none`. `self_hosted` is currently not supported and returns `not_implemented`; it may come back later on worker protocol v2 (see #442). On hosted computers, `packages` and
  `setup_commands` run before the first turn.
- Authenticate with `Authorization: Bearer` on every route except
  `/health` and `/metrics`.

`agent.model` is checked against the model list when an agent is
created or its model is edited. Missing model is `model_required`.
Unknown id is `model_not_found`. A later turn does not repeat that
check.

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
| `self_hosted` | OpenAI `codex exec-server` | Currently not supported in ApiPi (`not_implemented`); may come back later on worker protocol v2. Use `openai_hosted`. |
| Hosted files | Last until OpenAI's sandbox idle expiry | Last until `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour). Then Pi stops and `/workspace` is deleted. The next turn rebuilds skills, packages, and setup commands, and reloads the harness session cache. The session transcript stays. |
| `output_text.delta` | May be durable on their side | Live SSE only. Reconnect and export use `output_text.done` and items. |
| Thinking | May stream the full reasoning text | Stored preview (first 100 Unicode code points), duration, and reasoning token count. The full thinking text is not a public event. |
| SSE events | Typed OpenAI stream objects | `{type, seq, data, …}`. Extra OpenAI fields such as `delta` at the top level are omitted. Use raw SSE / `with_streaming_response`. |

## HTTP routes

| Route | Status |
| --- | --- |
| `POST/GET /v1/agents`, `GET/POST/DELETE /v1/agents/{id}` | Same API (field subset) |
| `GET /v1/models` | Same API when forwarding is on; `not_implemented` `forward_models` when off |
| `POST/GET /v1/agents/sessions`, `GET/POST/DELETE /v1/agents/sessions/{id}` | Same API |
| `POST/GET /v1/agents/sessions/{id}/events` | Same API (nested `events` and flat body) |
| `GET /v1/apipi/sessions/{id}/export` | ApiPi route. |
| `POST /v1/apipi/sessions/{id}/artifacts/{artifact_id}/download` | ApiPi route. |
| `GET /v1/apipi/agents/{id}/export` | ApiPi route. |
| `GET …/turns`, `GET …/items`, `GET/DELETE …/artifacts` | Same API |
| `GET /v1/apipi/usage` | ApiPi route (tokens and turn counts). |
| `/v1/apipi/templates`, `/v1/apipi/uploads` | ApiPi routes. |
| `POST /v1/apipi/auth/invalidate` | ApiPi route (drop cached auth identities for the caller's tenant). |
| `GET /health`, `GET /metrics` | ApiPi operator routes |
| `POST/GET/DELETE /v1/files`, `GET /v1/files/{id}/content` | Same API (purpose `user_data`, `assistants`, or `vision`; max `APIPI_MAX_FILE_BYTES`). `vision` must be an allowed image type within `APIPI_MAX_IMAGE_BYTES`. `GET /v1/files` pages with `limit` (1 to 100, default 20), `after`, `order`, and `purpose`, and lists only files of kind `file` unless `include_attachments=true`. |
| `GET /v1/apipi/files`, `GET /v1/apipi/sessions/{id}/files` | ApiPi routes. Files of every kind with filters, and the files bound to one session. |
| `POST/GET/DELETE /v1/skills` | Same shape, zip upload (no version endpoints). Max `APIPI_MAX_FILE_BYTES`. |
| `/v1/chat/completions` | Error (no such route) |
| ChatKit | Error (no such routes) |
| Vaults | `/v1/agents/vaults` and credentials. `static_bearer` for HTTP MCP. `environment_variable` on microVM sessions (`openai_hosted` on isolation `microvm`), with `secret_name`, `secret_value`, and `networking` as upstream. All credentials are a snapshot taken when the sandbox starts, as upstream. GET omits secret values. Secrets encrypted at rest. `mcp_oauth` is `not_implemented`. See [Vaults and credentials](vaults.md). |

## Extension table

New extension fields are grouped. Older flat fields stay flat.

| Kind | Name | Notes |
| --- | --- | --- |
| Field | `idle_ttl` | Agent and session. Flat. |
| Field | `user_id`, `org_id` | Session response. Flat. |
| Field | `session_defaults` | Agent. |
| Query | `include_attachments` | `GET /v1/files`. Without it the list has only files of kind `file`: what clients upload for agents and setup. `true` also lists attachments and the images users sent with messages. OpenAI has no such parameter and no file kinds. |
| Field | `environment.sandbox_size` | `S` / `M` / `L`. ApiPi extension. OpenAI `container_size` (`small` / `medium` / `large`) is the input and is stored as `sandbox_size`. |
| Field | `environment.container_size` | OpenAI `small` / `medium` / `large`. Stored as `sandbox_size`. |
| Field | `environment.sandbox_image` | Guest image id. |
| Field | `environment.sandbox` | Hosted runtime status. Null for `none`. |
| Field | `environment.directory` | Stored for the worker. Not returned on public session responses. |
| Metadata | `apipi.sandbox_image` | Stock SDK input. |
| Metadata | `apipi.sandbox_eager_boot` | Session or agent metadata override for eager boot. |
| Metadata | `apipi.system_prompt`, `apipi.idle_ttl` | Pi and idle overrides. Thinking is `reasoning.effort` (`none` is `off`); `metadata["apipi.thinking"]` is removed as client input (`400`) and stripped from response metadata. On update, `reasoning.effort` replaces the stored level and `null` clears it. |
| Metadata | `apipi.codemode`, `apipi.builtin_tools` | Codemode (`off`, `on`, `only`, default `off`) and built-in tools (`on`, `off`, default `on`). Session wins over agent. `apipi.builtin_tools=off` runs Pi without shell and file tools and without skills. Codemode `on` or `only` with built-ins off is `400` (`builtin_tools`). Built-ins are always off for `environment.type=none`. |
| Metadata | `apipi.git_username` | Vault credential metadata (`environment_variable` only). The user name the guest git credential helper sends for the credential's hosts. See [Vaults and credentials](vaults.md#environment_variable). |
| Metadata | `apipi.session_kind` | Removed former chat marker. Ignored now; use `environment.type=none`. |
| Event data | `data.sandbox` | Hosted `environment.*` events. |
| Route | `/v1/apipi/agents/{id}/export` | Agent zip. |
| Route | `/v1/apipi/sessions/{id}/export` | Session export. |
| Route | `/v1/apipi/sessions/{id}/artifacts/{artifact_id}/download` | Artifact download. |
| Route | `/v1/apipi/templates` | Agent templates. |
| Route | `/v1/apipi/uploads` | Presigned uploads (`file`, `attachment`, `image`, `skill`). |
| Route | `/v1/apipi/files` | Files of every kind, filtered by `kind`, `session_id`, `user_id`, `purpose`, and `filename` prefix. |
| Route | `/v1/apipi/sessions/{id}/files` | Files bound to a session. |
| Route | `/v1/apipi/usage` | Usage totals, including search calls and units. |
| Route | `/v1/agents/sessions` with `"environment": {"type": "none"}` | Text-only sessions. Function tools, HTTP MCP, and `web_search` only. |

## Agent fields and tools

| Field or tool | Status |
| --- | --- |
| `name`, `model`, `instructions`, `metadata` | Same API |
| `session_defaults` | ApiPi extension. Environment and `vault_ids` inherited by later sessions. |
| `idle_ttl` | ApiPi extension. Duration (`30m`, `1h`) or `0` to turn idle off. Stock SDKs can set `metadata["apipi.idle_ttl"]` instead. |
| `tools` type `function` | Same API |
| `tools` type `mcp` (flat OpenAI shape with `server_url`) | Same API |
| Nested MCP `transport` object | Error (`unknown_field`) |
| `connector_id`, `authorization` | Error (`not_implemented`) |
| `require_approval` other than `never` | Error (`not_implemented`) |
| stdio MCP | Removed |
| `service_tier` `null` or `auto` | Ignored. ApiPi has no tiers. Any other value is `not_implemented`. |
| `multi_agent`, `tool_search`, `programmatic_tool_calling` | Error (`not_implemented`) |
| `tools` type `web_search` | Same shape. Needs a search provider on the API, otherwise `400` with code `search_not_configured`. Search over MCP still works (example: Tavily). |
| `web_search` fields `type`, `filters.allowed_domains` | Supported. At most 10 domains. |
| `web_search` fields `search_context_size`, `user_location` | Error (`not_implemented`) |
| `web_search_preview` | Error (`not_implemented`) |
| Other `web_search` fields | Error (`unknown_field`) |
| Output item `web_search_call` | Same idea. `status` is `in_progress`, `completed`, or `failed`. `action` is `{"type": "search", "query": "..."}`. A failed call also has a short `error`. |
| First-party browser | Error; use the `browser` guest image and the built-in `browser` skill |
| Unknown JSON keys | Error (`unknown_field`) |

## Environment fields

| Field | Status |
| --- | --- |
| `type`: `openai_hosted`, `hosted`, `none` | Same shape, different backend for hosted; same API for `none`. `self_hosted` is currently not supported (`not_implemented`, may return on worker protocol v2). |
| `capability_directories` | Same API (skills on the computer) |
| `packages`, `setup_commands` | Same API on `openai_hosted` only; `400` on `none` |
| `sandbox_size` | ApiPi extension (`S` \| `M` \| `L`). Set `environment.container_size` (`small` \| `medium` \| `large`) or `environment.sandbox_size`. Top-level session `sandbox_size` is `unknown_field`. |
| `sandbox_image` | ApiPi extension. Stock SDKs can set `metadata["apipi.sandbox_image"]`. Top-level session `sandbox_image` is `unknown_field`. |
| `environment.sandbox` | ApiPi extension on hosted sessions: `state`, `reason`, `since`, `image`, `image_version`, `size`, `cold_boots`, `last_boot_ms`. Null for `none`. |
| `metadata["apipi.sandbox_eager_boot"]` | ApiPi extension. Overrides `APIPI_SANDBOX_EAGER_BOOT` for that agent or session. |
| `env` | Same API on `openai_hosted` only; reserved names `400`; `400` on `none` |
| `files` with `type: "inline"` or `type: "file_id"` | Same API on `openai_hosted` only. `file_id` mounts a Files API object. Other file types are `not_implemented`. |
| `network` | Same API on `openai_hosted` only. Session policy cannot widen `[sandbox.network]`. Isolation `none` cannot enforce `disabled` / `restricted`. |
| `skills` with `type: "skill_reference"` | Same API on `openai_hosted` only. Zip unpacks under `.agents/skills/`. Other skill types are `not_implemented`. |
| `environment_template_id`, `plugins` | Error (`not_implemented`) |
| Unknown JSON keys | Error (`unknown_field`) |

## Lifecycle

| Piece | OpenAI | ApiPi |
| --- | --- | --- |
| Session / transcript | Durable on their side | Durable in SQLite or Postgres until you delete the session. Export is enough to leave. |
| Computer / files | Cloud sandbox, about an hour idle. Create starts provisioning. | Hosted directory until sandbox TTL (default 1 hour), then a fresh `/workspace`. Boot is lazy at the first turn unless `APIPI_SANDBOX_EAGER_BOOT` or `metadata["apipi.sandbox_eager_boot"]` is on. There is no pause: a stop deletes files. `none` has no files. |
| `environment.status` | `provisioning`, `connected`, `failed` (and possibly `disconnected`; not verified against the full schema) | Hosted sessions return `provisioning`, `connected`, `disconnected`, or `failed`. `disconnected` covers not started and stopped. |
| `GET /v1/agents/environments/{id}` | OpenAI route. Exact fields beyond `status` were not verified against the API reference. | Returns `id`, `type`, `status`, and ApiPi `sandbox`. |
| Idle Pi | Their sandbox runtime | `none`: `APIPI_IDLE_TTL` (default 15 minutes) stops Pi. Hosted computers use sandbox TTL. The session row stays. The next turn starts a new Pi and reloads the cached session file. |
| Artifacts | `/workspace/outputs` published on turn complete | `/workspace/outputs` copied to the host store on turn complete. Immutable. Downloadable after the workspace expires. |
| Follow-up affinity | OpenAI's fleet | Any API replica, because the session is owned by a worker lease. See [multiple nodes](scale.md). |

## Errors

| Case | Type | Code |
| --- | --- | --- |
| Extra JSON key | `invalid_request` | `unknown_field` |
| Known OpenAI field we skip | `not_implemented` | The field name (`multi_agent`, `files`, …) |
| Missing or bad bearer | `invalid_request` | `unauthorized` (`401`) |
| Id on another tenant | `invalid_request` | `not_found` (`404`) |
| Unknown `agent.model` on agent write | `invalid_request` | `model_not_found` (`400`) |
| Model list unreachable on agent write | `invalid_request` | `model_host_unreachable` (`400`) or `model_host_unauthorized` (`401`) |
| Host rejects the model during a turn | `api_error` | `model_host_error` on the `502` body in this release (`detail_code` is the specific code; session stays `idle`) |
| Known image, no worker has it | `api_error` | `image_unavailable` (`503`) |
| Nested `events` length not 1, or mixed flat+nested body | `invalid_request` | `validation_error` |
| `input_image` with an `http` or `https` URL | `invalid_request` | `invalid_request` |
| `input_image` with neither `image_url` nor `file_id`, or with both | `invalid_request` | `validation_error` (nested body) or `invalid_request` |
| `input_image.file_id` of another tenant or unknown | `invalid_request` | `not_found` (`404`) |
| `input_image.file_id` that is not an allowed image type | `invalid_request` | `invalid_request` |
| Image larger than `APIPI_MAX_IMAGE_BYTES` | `invalid_request` | `payload_too_large` (`413`) |
| Image sent to a model that does not list `image` in its registry `input` | `invalid_request` | `unsupported_input` |
| `input_file` with a type the model cannot read without a computer (pdf, xlsx, docx, zip, …), or a text file that is not UTF-8 | `invalid_request` | `unsupported_file_type` |
| Text `input_file` larger than `APIPI_MAX_INLINE_FILE_BYTES` | `invalid_request` | `payload_too_large` (`413`) |
| `input_file` with `file_data` or `file_url` | `not_implemented` | `file_data` or `file_url` |
| `input_file` larger than `APIPI_MAX_FILE_BYTES`, or attachments that with the agent inputs exceed `APIPI_MAX_WORKSPACE_BYTES`, in a session with a computer | `invalid_request` | `payload_too_large` (`413`) |
| Other non-text input parts | `not_implemented` | The part type |

The envelope is `{ "error": { "type", "code", "message" } }`. When
create already stored a session and the first turn failed, the error
also has `session_id` and HTTP status `502`, plus `detail_code`,
`failure_source`, `upstream_status`, and `retryable` when the failure
was classified. See [API errors](api.md#errors) and
[failure codes](errors.md).

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

An `input_image` part takes the two forms of the OpenAI Responses API:
`{"type": "input_image", "image_url": "data:image/png;base64,…"}` or
`{"type": "input_image", "file_id": "file-…"}` for a file uploaded with
`POST /v1/files`. As with OpenAI, upload such an image with purpose
`vision`: ApiPi then stores it as a file of kind `image`, which the
default `GET /v1/files` list leaves out. `detail` is accepted and
ignored. Remote `http` and `https` image URLs are not supported. Both
forms are stored as Files API objects, and the user item lists the
image as `{"type": "input_image", "file_id"}`. See
[API](api.md#events) for the limits.

An `input_file` part takes the `file_id` form of the OpenAI Responses
API: `{"type": "input_file", "file_id": "file-…"}`, with an optional
`filename` that replaces the stored file name. `file_data` and
`file_url` are not implemented; upload the file first and send its id.
In a session without a computer (`environment.type` `none`), a text
file goes to the model as text with its file name, and an image file
goes to the model like `input_image`. Unlike OpenAI, ApiPi does not
read PDF or other documents for the model; such a file returns `400`
with code `unsupported_file_type`. The user item lists the part as
`{"type": "input_file", "file_id", "filename"}`. In a session with a
computer, any file type is accepted and goes to the workspace under
`attachments/` instead of the model, as in a code interpreter
container. The agent opens it with its tools, and the user item also
has the workspace `path`. See [API](api.md#events) for the types and
limits.

SSE events use ApiPi public types (`agent.session.created`,
`agent.session.turn.output_text.done`, and the rest listed on
[API](api.md#events)). Each event has `seq`. Payload fields live under
`data`. Typed OpenAI stream objects that expect top-level `delta` or
`item_id` will not see those keys; parse `data` or use
`with_streaming_response`.

## How to verify

1. Install and serve the gateway ([Install](install.md)).
2. Run `examples/sessions/openai_sdk.py` as on [Using the API](using.md).
3. Send a follow-up with the nested `events` body (SDK
   `sessions.events.create`) or the flat curl example on that page.
4. Confirm unknown keys return `unknown_field` and `multi_agent` returns
   `not_implemented`.

Field-level HTTP reference remains [API](api.md). Computers and TTL are
on [environments](environments.md) and [concepts](concepts.md).
