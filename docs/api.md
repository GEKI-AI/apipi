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
require that user, and attachments and images of another user are not
visible (see [files](#files)). An id that belongs to another tenant or
another user returns `404`, not `403`.

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
`tools` (function, mcp, web_search), `session_defaults`,
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

`tools` accepts `{"type": "web_search"}` with an optional
`filters.allowed_domains` list (at most 10 domains). It turns on the
built-in search tool. When the operator has not configured a search
provider for the caller, create and update return `400` with code
`search_not_configured`. The tool is never dropped silently.
`search_context_size`, `user_location`, and the `web_search_preview`
type return `not_implemented`. See [tools](tools.md#web-search).

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

`vault_ids` on session create attaches vaults. A `static_bearer`
credential gives an HTTP MCP server its bearer on the host broker. An
`environment_variable` credential lets code in a microVM sandbox call
HTTPS APIs with a key it never holds. Secrets are encrypted at rest.
GET of a vault or credential never returns them. See
[Vaults](#vaults) and [Vaults and credentials](vaults.md).

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

Create a vault with `name` and `metadata`. Every query is
tenant-scoped. A vault from another tenant is `404`. Deleting a vault
deletes its credentials. The guide with examples for GitHub, Forgejo,
GitLab, and other services is [Vaults and credentials](vaults.md).

A credential has `name`, `metadata` (a key-value map, as on vaults),
and `auth`. `auth.type` selects the kind:

| `auth.type` | Write fields | Returned in `auth` | Used by |
| --- | --- | --- | --- |
| `static_bearer` | `mcp_server_url`, `token` | `type`, `mcp_server_url` | The host broker, for the HTTP MCP server with that URL or `credential_id` |
| `environment_variable` | `secret_name`, `secret_value`, `networking` (`{"type": "limited", "allowed_hosts": [...]}`) | `type`, `secret_name`, `networking` | The egress gateway of a microVM sandbox, for HTTPS requests to `allowed_hosts` |
| `mcp_oauth` | | | `400` with type `not_implemented` |

The store keeps `token` and `secret_value` as AES-256-GCM ciphertext
(`APIPI_VAULT_MASTER_KEY`). List and get never return them.

`environment_variable` fields:

| Field | Rule |
| --- | --- |
| `secret_name` | `^[A-Za-z_][A-Za-z0-9_]*$`, unique in the vault. Reserved names (`OPENAI_*`, `APIPI_*`, `PI_*`, `CODEX_*`, `GIT_*`, `AGENT_BROWSER_*`, `PATH`, `HOME`, the certificate and cache variables, and the guest environment deny list) are `400`. The full list is in [Vaults and credentials](vaults.md#environment_variable). |
| `secret_value` | 8 to 16,384 characters of printable ASCII (`!` to `~`), without spaces or control characters. |
| `networking.type` | `limited` |
| `networking.allowed_hosts` | 1 to 100 exact hostnames, stored in lowercase. No scheme, port, path, wildcard, or IP address. |
| `metadata["apipi.git_username"]` | Optional. The user name the guest git credential helper sends for these hosts. |

Update (`POST /v1/agents/vaults/{vault_id}/credentials/{id}`) keeps the
id and the type. It can change `name`, replace `metadata`, and replace
`token` or `secret_value`. Changing `auth.type`, `secret_name`, or
`networking` is `400`; create a new credential instead. Static bearer
credentials keep their earlier validation and error codes.

Credential errors:

| Status | Code | When |
| --- | --- | --- |
| `400` | `invalid_request` | A field breaks a rule above, or an update tries to change `auth.type`, `secret_name`, or `networking`. The message names the field. |
| `400` | `unknown_field` | An unknown key in an `environment_variable` `auth` or `networking` object. |
| `400` | `validation_error` | A `static_bearer` body without `mcp_server_url` or `token`, or an unknown `auth.type`. |
| `400` | `secret_name_collision` | The vault already has a credential with that `secret_name`. |
| `400` | `mcp_oauth` (type `not_implemented`) | `auth.type` `mcp_oauth`. |
| `404` | `not_found` | Unknown vault or credential, or one from another tenant. |

Session create checks the environment credentials of the attached
vaults, after merging the agent's `session_defaults.vault_ids`. Each
rule is `400`: environment credentials on `environment.type` `none`
(`credential_not_allowed`), with `network.access` `disabled`
(`credential_not_allowed`), two attached credentials with the same
`secret_name` (`secret_name_collision`), a `secret_name` that is also
an `environment.env` key (`secret_name_collision`), and, when the
operator TAP allowlist is on, a credential host the operator does not
allow (`credential_host_not_allowed`). With `network.access`
`restricted`, the credential hosts are added to the allowed hostnames
when the sandbox starts. All vault credentials are a snapshot taken
when the sandbox starts. See
[Vaults and credentials](vaults.md#when-secrets-are-read).

A turn, follow-up, or sandbox start for a session with environment
credentials needs a worker that runs isolation `microvm`. If the worker
for the session runs another isolation, the request fails with `400`
and code `credential_not_allowed`, and the message names the isolation.
A worker from an older ApiPi version without the protocol feature
`env_credentials` gets `501` with code `unsupported_op` instead. The
API does not check the run mode at session create, because
`apipi serve` has no run mode of its own; it learns the isolation from
the worker that takes the session.

## Files

| Method | Path |
| --- | --- |
| `POST` | `/v1/files` |
| `GET` | `/v1/files` |
| `GET` | `/v1/files/{file_id}` |
| `GET` | `/v1/files/{file_id}/content` |
| `DELETE` | `/v1/files/{file_id}` |
| `GET` | `/v1/apipi/files` |
| `GET` | `/v1/apipi/sessions/{session_id}/files` |

Upload is multipart form data with `file` and `purpose`. Accepted
purposes are `user_data`, `assistants`, and `vision`. Other purposes
return `not_implemented`. `vision` is for an image a client uploads to
send later as `input_image` by `file_id`. Its content type must be in
`APIPI_IMAGE_MIMES` (`400` otherwise) and its size within
`APIPI_MAX_IMAGE_BYTES` (`413` with code `payload_too_large` otherwise),
and it creates a file of kind `image`. The object is `{ id, object: "file", bytes,
created_at, filename, purpose, status }`. `created_at` is a Unix
timestamp. Ids look like `file-` plus hex. Bytes live in the same
object store as artifacts (`APIPI_ARTIFACT_STORE`). Metadata is in
Postgres. The upload cap is `APIPI_MAX_FILE_BYTES` (default 50 MiB).
A larger body returns `413` with code `payload_too_large`. A file
from another tenant is `404`. Attach an uploaded file on session
create with `environment.files` `{ "type": "file_id", "file_id":
"…", "path": "/workspace/…" }`.

Every file has a `kind` that says what it is for. The kind is set when
the file is created and does not change:

| Kind | Created by |
| --- | --- |
| `file` | `POST /v1/files` with purpose `user_data` or `assistants`, and a [presigned upload](#uploads) with `purpose: "file"`. Files for agents and setup. |
| `attachment` | A presigned upload with `purpose: "attachment"`: a file an end user attaches to a message. |
| `image` | An image sent with a message. `POST /v1/files` with purpose `vision` and a presigned upload with `purpose: "image"` create one (purpose `vision`). The gateway also stores each `input_image` data URL as one (purpose `user_data`, see [events](#events)). |

A file also keeps the `user_id` of the auth identity that uploaded it,
the same way a session keeps its `user_id`. Images take the `user_id`
of their session. The value is null when the identity has no
`user_id`.

Files of kind `attachment` and `image` are user files. When the auth
identity has a `user_id`, a user file is visible only when its
`user_id` is the same or null. A user file of another user behaves
like a file of another tenant: `GET /v1/files/{file_id}`, `/content`,
`POST /v1/apipi/files/{file_id}/download`, and `DELETE
/v1/files/{file_id}` return `404`, the lists leave it out, an `after`
cursor on it is `400`, and an `input_image` or `input_file` with its
`file_id` or an
`environment.files` entry with its `file_id` (on session create, or in
`session_defaults` on agent create and update) is `404`. Files of kind
`file` are agent files and stay visible to every identity of the
tenant, whoever uploaded them, so an agent file in `session_defaults`
works in every user's sessions. Agent export
(`GET /v1/apipi/agents/{agent_id}/export`) and `POST
/v1/apipi/templates` read the files of the agent's `session_defaults`
with the same rule, so an agent that still references a user file of
another user (saved before such files became agent files) is `404`
there. An identity without `user_id` sees every file of the tenant.

A file can be bound to one or more sessions. An image is bound to the
session whose message carried it, and an image sent by `file_id` is
bound to that session too, whatever its kind. So is a file sent as
`input_file` (see [events](#events)). A binding has a `path`
(the workspace path of a file attached in a session with a computer,
for example `attachments/report.xlsx`, and null otherwise) and the
`item_id` of the user item the file came with. `item_id` is set when
the worker stores that user item and the item lists the file, so it is
null until then, and stays null for a file that no item lists.

`GET /v1/files` has the OpenAI shape `{ object: "list", data, first_id,
last_id, has_more }` and lists only files of kind `file`. Add
`include_attachments=true` to list attachments and images as well. The
list is paginated:

| Parameter | Meaning |
| --- | --- |
| `limit` | Page size, 1 to 100, default 20. Other values are `400`. |
| `after` | A file id from the previous page. The next page starts after it. An id that is not a file of the tenant (or, for the session route, not bound to the session), that is a user file of another user, or that the authorization hook's `file.list` filter does not allow, is `400`. |
| `order` | `desc` (newest first, the default) or `asc`. |
| `purpose` | Only files with this purpose. |

Files are ordered by `created_at` and then by id, so pages stay stable
while new files arrive. `has_more` is true when another page follows.
`first_id` and `last_id` are the first and last ids of `data`, or null
for an empty page.

`GET /v1/apipi/files` lists every kind by default and takes the same
`limit`, `after`, `order`, and `purpose`, plus these filters:

| Parameter | Meaning |
| --- | --- |
| `kind` | `file`, `attachment`, or `image`. Repeat it for several kinds (`?kind=attachment&kind=image`). Another value is `400`. |
| `session_id` | Only files bound to this session. The session must be readable by the caller, as for `GET /v1/apipi/sessions/{session_id}/files` (`session.read`). A session of another tenant, or of another user when the identity has a `user_id`, is `404`. |
| `user_id` | Only files with this `user_id`. It never matches files of another tenant. When the identity has a `user_id`, it never matches user files of another user, so `user_id` of another user lists only that user's files of kind `file`. |
| `filename` | Only files whose name starts with this text. The match is case-sensitive. |

Each object is the file object plus `kind`, `user_id`, and
`content_type`.

`GET /v1/apipi/sessions/{session_id}/files` lists the files bound to one
session with the same `limit`, `after`, and `order`. Each entry is
`{ file_id, kind, filename, bytes, content_type, path, item_id,
created_at }`, where `created_at` is the time of the binding and the
order follows it. `first_id` and `last_id` are file ids. The session
must be readable by the caller, like the other session routes, and
user files of another user are left out. The authorization hook's
`file.list` filter applies to all three lists.

Deleting a session deletes its bindings and every bound file of kind
`attachment` or `image` that no other session still uses, both the row
and the bytes. Files of kind `file` are never deleted with a session.
Deleting a file deletes its bindings. A stored item keeps the
`file_id`, and reading the file then returns `404`.

An attachment that is not bound to any session is deleted, with its
bytes, once it is older than `APIPI_ATTACHMENT_TTL` (default 24 hours).
The API checks once an hour. An attachment used as an agent or session
input becomes a file of kind `file`, so the check never deletes it:
this happens when its id is in `environment.files` (`type: "file_id"`)
of a session create, including files that come from the agent's
`session_defaults`, or in `session_defaults` saved on an agent (also
an agent made from a template). A file that an agent's
`session_defaults` references is an agent file, so on agent create and
update an image there becomes kind `file` as well, and every user of
the tenant can start a session from that agent and export it. An image
in `environment.files` of a session create stays an image. A user file
of another user is `404` in both places, so a caller never changes its
kind.

Browser and BFF uploads that must not proxy bytes through the gateway
use [presigned uploads](#uploads) instead of this multipart route.
`GET /v1/files/{id}/content` still streams through the gateway. The
response uses `Content-Disposition: attachment` with the stored file
name, including an RFC 5987 `filename*` when the name is not ASCII, and
`X-Content-Type-Options: nosniff`. `POST /v1/apipi/files/{id}/download`
returns a short-lived GET URL when the artifact store is S3.

Every read of stored file or skill bytes on the gateway stops after the
stored `bytes` plus one byte: `GET /v1/files/{file_id}/content`, the
UTF-8 check of an `input_file` text file, `environment.files` and
`environment.skills` at session create, agent export, and `POST
/v1/apipi/templates`. An object whose size is not the stored `bytes`
returns `503` with code `artifact_store`. A worker also stops reading a
file after its size, so an object of another size fails the turn with
code `artifact_store`.

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

Create takes `purpose` (`file`, `attachment`, `image`, or `skill`),
`filename`, `bytes`, optional `content_type`, and optional
`file_purpose`. `attachment` and `image` use the same store as `file`.
On complete, `file` creates a file of kind `file`, `attachment` a file
of kind `attachment`, and `image` a file of kind `image` (see
[files](#files)). All three keep the `user_id` of the identity that
created the upload. `file_purpose` is the Files API purpose:
`user_data` (the default) or `assistants` for `file` and `attachment`,
and always `vision` for `image`. Any other value with `image` is `400`,
and `vision` with `file` or `attachment` is `400`. Create and complete
both check `file_purpose` this way, and complete uses the value sent
with complete. For `image`, the
declared `content_type` must be in `APIPI_IMAGE_MIMES` (`400`
otherwise) and `bytes` within `APIPI_MAX_IMAGE_BYTES` (`413` with code
`payload_too_large` otherwise). Complete checks the stored size against
the same limit again and deletes an object that is too large. Complete
before PUT is `400` with code `upload_incomplete`. Wrong tenant is
`404`. When the identity has a `user_id`, complete also needs the
upload to have the same `user_id` or none, for every `purpose`
including `skill`; another user's upload is `404`. The Pi harness
session cache is not exposed this way.

The response of create is a PUT URL, the `headers` to send with it, and
`expires_at`. The URL points at an upload key of its own under the
`uploads` prefix of the bucket, next to `files` and `skills` (see
`APIPI_S3_PREFIX` in [configuration](config.md)), and not at the file
or skill. Its signature covers `Content-Type` and `Content-Length`, so
the body must have exactly `bytes` bytes and the declared content type.
Object storage rejects a PUT of another size, usually with `403`. Most
HTTP clients, and browsers, set `Content-Length` from the body
themselves.

PUT the bytes, then complete. Complete checks the upload key with
`HeadObject`. The size must be within `bytes` and
`APIPI_MAX_FILE_BYTES` (`413` with code `payload_too_large` otherwise,
and the uploaded object is deleted). Complete then copies the object
inside the bucket (`CopyObject`) to the final key of the file or skill,
checks a skill zip like `POST /v1/skills` does, writes the Files or
Skills metadata, and deletes the upload key. The final key never gets a
PUT URL. So the bytes that a `file_id` or `skill_id` stands for cannot
change after complete. A later PUT to the same URL, which stays valid
until `expires_at`, only writes the upload key again. It does not
change what `GET /v1/files/{file_id}/content`, `input_file`,
`input_image`, `environment.files`, or `environment.skills` deliver.
Complete of an upload that is already complete returns the existing
file or skill. Two completes of one upload at the same time do not
both copy: the second waits for the first and then returns its file or
skill. If the copy fails, complete returns `503` with code
`artifact_store`, stores nothing, and the same complete can be sent
again. When a later step fails after the copy, the copied object is
deleted again. The copy only takes the object that complete checked
(`CopySourceIfMatch` with the ETag of `HeadObject`), so a PUT between
the check and the copy also fails the copy, and complete can be sent
again. If object storage returns no ETag, the copy runs without that
condition, and the signed `Content-Length` still keeps the object at the
declared size.

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
is the usual bearer. The host call uses the same model key Pi uses:
`OPENAI_API_KEY_OVERWRITE` when that is set, otherwise the
`model_credential` callback, otherwise the request bearer (see
[auth](auth.md#model-credential)).
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

`DELETE` removes the session, its workspace, its artifacts, and the
attachments and images that only this session uses (see
[files](#files)).

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
one shape or the other, not both. A body with an `events` key is the
nested shape, so a flat field such as `type` next to `events` is `400`
with code `unknown_field`. A body without `events` is the flat shape.
An error names the field of the shape that was sent: an invalid input
part in the nested shape gets the same code and message as in
`input` on session create.

A message event starts a turn. Nested form: `type`
`agent.session.input.message` and `input` with a `user` message whose
`content` has `input_text`, `input_image` (for a vision model), and
`input_file`.
A model that is not in the registry, or whose `input` does not include
`image`, returns `400` with code `unsupported_input`. Flat form: `type`
`agent.session.input.message` and `content` or `text`.

An `input_image` part has exactly one of two fields. `image_url` is a
`data:` URL with a base64 image (`png`, `jpeg`, `webp`, or `gif`, as
set by `APIPI_IMAGE_MIMES`). Remote `http` and `https` URLs are
rejected. `file_id` is the id of a Files API object (`POST /v1/files`),
as in the OpenAI Responses API. The file must belong to the tenant
(`404` otherwise), its content type must be an allowed image type
(`400` otherwise), and its size must be within `APIPI_MAX_IMAGE_BYTES`
(`413` with code `payload_too_large` otherwise). A part with neither
field, or with both, is `400` with code `invalid_request` and the
message `input_image needs image_url or file_id`. `detail` is accepted
and ignored. A message may carry up to `APIPI_MAX_IMAGES` images, and
each may be up to `APIPI_MAX_IMAGE_BYTES`, in either form. A data URL counts against
the request body limit (`APIPI_MAX_REQUEST_BYTES`, 1 MiB by default),
so upload larger images as files and send `file_id`. Upload them with
purpose `vision` (`POST /v1/files`) or as a presigned upload with
`purpose: "image"`, so they are files of kind `image` and stay out of
the default `GET /v1/files` list.

The gateway stores each data URL image as a Files API object (kind
`image`, purpose `user_data`, filename `image`, and the `user_id` of the
session) before the turn starts. Every image of the message, also one
sent by `file_id`, is bound to the session (see [files](#files)). The user
item keeps `{"type": "input_image", "file_id": "file-…"}`, never the
base64. The worker that runs the turn reads the image from the store
and passes it to the model. A message that, with the session context,
is too large to send to a worker fails before the turn with `413` and
code `payload_too_large`. Images and files never count toward that
limit.

An `input_file` part is `{"type": "input_file", "file_id": "file-…"}`
with an optional `filename`. `file_id` is the id of a file of the
tenant that the caller can see (`404` otherwise, see
[files](#files)), uploaded with `POST /v1/files` or a
[presigned upload](#uploads), for example with `purpose: "attachment"`.
`filename` replaces the stored file name in the prompt, in the item,
and in the workspace path. A part without `file_id` is `400` with
code `invalid_request` and the message `input_file needs file_id`;
`file_data` and `file_url` are not implemented. A message may carry up to `APIPI_MAX_FILES_PER_MESSAGE`
(default 10) `input_file` parts, and a message with only `input_file`
parts starts a turn too.

In a session without a computer (`environment.type` `none`), the
gateway decides from the content type and the name of the file how it
goes to the model, before the turn starts:

| File | To the model |
| --- | --- |
| Content type in `APIPI_IMAGE_MIMES` | As an image, like `input_image` with that `file_id`. The model must accept images (`400` `unsupported_input` otherwise), the size must be within `APIPI_MAX_IMAGE_BYTES`, and the file counts toward `APIPI_MAX_IMAGES`. |
| Content type `text/*`, `application/json`, `application/xml`, `application/yaml`, `application/x-yaml`, or `application/javascript` | As text. |
| No content type or `application/octet-stream`, and a name ending in `.txt`, `.md`, `.markdown`, `.csv`, `.tsv`, `.json`, `.jsonl`, `.yaml`, `.yml`, `.xml`, `.toml`, `.ini`, `.log`, `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.sql`, `.sh`, `.html`, `.css`, `.java`, `.go`, `.rs`, `.rb`, `.php`, `.c`, `.h`, `.cpp`, `.cs`, `.kt`, or `.swift` | As text. |
| Anything else (pdf, xlsx, docx, zip, …) | Not at all: `400` with code `unsupported_file_type`, and a message that says the session needs a computer for that file type. |

A text file must be at most `APIPI_MAX_INLINE_FILE_BYTES` (256 KiB by
default, `413` with code `payload_too_large` otherwise), and its bytes
must be UTF-8 (`400` `unsupported_file_type` otherwise). The gateway
reads the stored bytes once to check this. The model gets the text at
the place of the part, with the file name, joined to the `input_text`
parts by a newline:

```
<file name="notes.md">
…content…
</file>
```

Each file is bound to the session without a `path` (see
[files](#files)) and keeps its kind, so an uploaded file of kind `file`
stays a `file` and an attachment stays an attachment. The user item
keeps `{"type": "input_file", "file_id": "file-…", "filename": "…"}`,
never the text. The worker reads the file from the store, like an
image.

In a session with a computer (`openai_hosted`, which is also the
default when create omits `environment`), every file type is accepted,
and the file goes to the workspace instead of the model. The gateway
binds it to the session with the path `attachments/<filename>`, or a
free name like `attachments/report (2).xlsx` when the session already
has a file at that path. A `file_id` that is already bound to the
session with a path keeps it. Before Pi starts the turn, the worker
writes the file into the workspace and replaces what is at that path,
so attaching the same `file_id` again resets the file to its original
content. If the file cannot be copied into a running sandbox, the turn
does not start and the session gets an error with code
`attachment_push_failed`; send the message again. The prompt gets one
line per file at the place of the part:

```
Attached: attachments/report.xlsx (xlsx, 240 KB)
```

The file keeps its kind and is not added to `environment.files`. After
a sandbox restart or a TTL wipe, the next turn restores every file
bound to the session under the same path, and a file deleted from the
Files API is simply not restored. Each file must be within
`APIPI_MAX_FILE_BYTES`, and the agent inputs (`environment.files`) and
all attached files of the session together must fit
`APIPI_MAX_WORKSPACE_BYTES`: otherwise the request fails with `413` and
code `payload_too_large` before the turn starts. The user item keeps
`{"type": "input_file", "file_id": "file-…", "filename": "…", "path":
"attachments/…"}`, so a client can show the attachment in the history.
See [environments](environments.md#attachments).

Follow-up messages work the same way after the session is idle. A message while
the session is `in_progress` cancels that turn (or fails it if the
process no longer owns it) and starts a new turn, so a hung Pi cannot
block the next command. The message is checked first: its images and
files are stored and bound, and the limits are checked, before the
running turn is cancelled, so a message that fails with `4xx` leaves
that turn running. `GET` of a session that is `in_progress` with
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
short preview, not as live deltas. The worker sends
text fragments as ephemeral messages over its socket (coalesced over
about 40ms), and the API fans them out over the event bus, so token
streaming works on any replica with no sticky routing; a delta that
arrives after its turn already committed `output_text.done` is
dropped. See [multiple nodes](scale.md).

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
`mcp_list_tools`, `command_execution`, `web_search_call`. Thinking is not an item. `GET /items` does not
list it.

A `web_search_call` item is one call of the built-in `web_search`
tool. It arrives in `agent.session.turn.item.added` and
`agent.session.turn.item.done` like other items. `data` has `status`
(`in_progress`, `completed`, or `failed`) and `action`, which is
`{"type": "search", "query": "..."}`. A failed call also has a short
`error` text. The result list the model saw is not stored in the item.

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

Turns, items, and artifacts are listed in the order they were created,
oldest first. Each new turn, item, or artifact gets a `created_at` that
is later than the `created_at` of every earlier one of the same kind
in its session. This holds even when the server's wall clock steps
back, for example after an NTP or virtual machine time correction, and
when API replicas disagree about the time. Right after such a step,
`created_at` can be a little ahead of the wall clock. Rows that share
a `created_at`, which only data stored before this rule can have, are
ordered by id. The export uses the same order.

When a turn completes, files under `outputs/` on the computer are
copied into the artifact store. Copies are immutable and include
`turn_id`. A later turn that writes the same path publishes another
artifact, unless the bytes are the same as in the newest artifact at
that path, which is the one created last in the order above. When the
worker reports that newest artifact and the next write of the path
together, for example after a reconnect, the next write is published
even if its bytes are the same (see
[Workers](workers.md#artifacts)). Rows
already stored with a path under `artifacts/` stay readable; new publishes use `outputs/`. `GET` content works as soon as the turn has
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
The response has token totals, `turns`, `search_calls`, and
`search_units`. Counters, not USD. See [usage](usage.md#query).

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
| `none` | No computer. Function tools, HTTP MCP, and `web_search` only (anything else is `400`). |
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
use guest RAM because `/workspace` is a tmpfs. `openai_hosted`
sessions are always placed on a `microvm` worker, and the guest root is
read-only, so `packages.system` returns `400` from the API whenever
`type=openai_hosted`, and the message tells you to bake those packages
into a guest image. `setup_commands` is an ordered list of
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
