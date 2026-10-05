# Worker protocol

This page is the normative specification of worker protocol v2: the
messages that cross the WebSocket `/internal/worker` between an ApiPi API
process and a worker. A developer who writes a worker in another language
needs this page, the JSON Schema in `docs/worker-protocol/schema/`, and the
golden transcripts in `tests/fixtures/worker-protocol/`. Nothing else is
needed to speak the protocol.

Other pages have other jobs. [Workers](worker-concepts.md) explains why the
API and the workers are separate processes. [Sandbox workers](workers.md) is
the operator reference: tokens, TLS, placement, drain, and settings.
[ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md)
records the decision and the reasons. The Python models in
`src/apipi/protocol/` are the reference implementation. When the code and
this page disagree, the code is wrong and the difference is a bug, except
where [Known differences](#known-differences) lists it.

The words MUST, MUST NOT, SHOULD, and MAY have their usual meaning in a
specification. "Sender" and "receiver" mean the peer that writes or reads a
message. "The worker" and "the API" are the two peers.

## Transport and framing

| Rule | Value |
| --- | --- |
| Transport | WebSocket (RFC 6455). The worker dials the API. The API never dials a worker. |
| URL | `<api-url>/internal/worker`, with `ws://` or `wss://`. One URL, one socket per worker process. |
| Frames | Text frames only. A binary frame is skipped and counted by the receiver. |
| Encoding | One JSON value per frame, in UTF-8. The value MUST be an object. A frame that is not valid JSON or not an object is skipped and counted. It does not close the socket. |
| Frame size | The unit of every size limit is the number of bytes of the UTF-8 text of the frame as sent. One envelope MAY be at most 1,048,576 bytes. One command frame MAY be at most 262,144 bytes. The API closes a socket on a frame over 4,194,304 bytes (close code `1009`). |
| Compression | The protocol does not depend on `permessage-deflate`. Sizes are always measured before compression. |
| Authentication | The worker sends `Authorization: Bearer <token>` in the WebSocket handshake. The token starts with `apipi_wk_`. It is valid only on this route. |
| TLS | A worker MUST use `wss://` for any API host that is not loopback. Mutual TLS is optional: the worker presents a client certificate and the API side (usually a proxy) verifies it. The message types do not change with TLS. |
| Ping | The worker sends a WebSocket ping every 5 seconds and closes the socket when no pong arrives within 10 seconds. A peer MUST answer pings, which means it MUST keep reading its socket. |

A worker MUST NOT do slow work in the loop that reads the socket. It parses a
frame and hands the work to a task. The reply a task waits for can only be
delivered by the read loop, and a socket that is not read stops answering
pings.

## Data types

| Type | Rule |
| --- | --- |
| UUID | Lowercase, hyphenated, 36 characters (`123e4567-e89b-12d3-a456-426614174000`). A receiver matches ids as strings. A peer MUST echo an id exactly as it received it. |
| Time | RFC 3339 in UTC with a trailing `Z` and 3 fractional digits, for example `2026-01-02T03:04:05.678Z`. A sender MUST use this form. A receiver SHOULD also accept an offset form such as `+00:00` and any number of fractional digits, because older peers send it. |
| Duration | A field name ends in its unit. `_seconds` is a JSON number (it may have a fraction). `_ms` is a JSON integer. |
| Epoch time | `idle_since_epoch` is the one exception to the time rule: it is seconds since the Unix epoch as a JSON number. Its name says so. |
| Integer | A JSON number without fraction or exponent. A sender MUST NOT send an integer as a string. A receiver SHOULD reject a string where an integer is required. |
| Boolean | `true` or `false`. |
| String | UTF-8. A string MUST NOT contain a lone surrogate. |
| Absent and `null` | A receiver treats an absent field and a field set to `null` the same way, with the exception of `turn_id` in an envelope, where `null` means "not turn scoped". A sender writes only the fields it has a value for, except where a table says a field is required. |
| Unknown fields | A receiver ignores any field it does not know, in every message, payload, and the command context. A sender MUST NOT rely on a receiver rejecting one. |
| Sizes | Bytes. Token counts and similar are plain integers. |

## Message kinds

Every frame is one of two kinds.

* An **envelope** is a frame that has the field `v`. Envelopes go only from
  the worker to the API. They carry the results of work and are described in
  [Sequencing and delivery](#sequencing-and-delivery).
* A **control message** is any frame without `v`. Its `type` field names the
  message. A command is a control message with `type` `command` and an `op`.

A receiver tells the two kinds apart by `v` alone. This matters because some
names exist in both kinds: the envelope type `event` is a session event, and
the envelope type `error` is a worker error, while `error` as a control
message is the object the API sends before it closes a socket. The
direction also helps: the API never sends envelopes, and the worker never
sends the error object.

A frame with `v` other than `2`, or with a `type` the receiver does not
know, is not an error. A receiver counts it and ignores it. For an envelope
this has a consequence: the receiver does not ack it on its own, and a later
cumulative ack passes over it.

### Worker to API

| `type` | Kind | What |
| --- | --- | --- |
| `register` | control | First message of a connection. |
| `store.proof` | control | Proof that the worker sees the shared store root. |
| `heartbeat` | control | Renews every lease of the worker and refreshes its advertisement. |
| `lease.ack` | control | The worker received a command. |
| `lease.release` | control | The worker dropped a session. |
| `inventory` | control | The live set of the worker. |
| `sandbox.seen` | control | Ids of the live sandboxes. |
| `search.request` | control | One `web_search` call. |
| 14 durable types | envelope | `item.added`, `item.done`, `turn.status`, `usage`, `event`, `session.status`, `artifact.presign`, `artifact.completed`, `session.stopped`, `workspace.reaped`, `lifecycle.start`, `lifecycle.stop`, `error`, `sandbox.status`. |
| 2 ephemeral types | envelope | `delta.text`, `delta.reasoning`. |

### API to worker

| `type` | Kind | What |
| --- | --- | --- |
| `hello` | control | Answer to a valid `register`. |
| `error` | control | A rejected handshake. The socket closes right after it. |
| `command` | control | A unit of work for a lease. |
| `ack` | control | Cumulative ack of durable envelopes. |
| `artifact.presign.reply` | control | Answer to one `artifact.presign` envelope. |
| `search.reply` | control | Answer to one `search.request`. |
| `inventory.reply` | control | Answer to `inventory`. |
| `lease.revoke` | control | A lease is no longer valid. |

### Handshake messages

#### `register` (worker to API)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `register`. |
| `protocol` | integer | yes | MUST be `2`. Any other value is rejected as `unsupported_protocol`. |
| `id` | UUID | no | The worker id. When absent, the API uses the id its token is bound to, or mints one on first use. A different id than the bound one is rejected as `token_bound`. |
| `run_mode` | string | yes | The process backend of the worker: `none`, `microvm`, or a custom class name. Not empty. |
| `accepts` | list of string | no | Session kinds the worker runs: `none`, `microvm`. Default: both for `run_mode` `microvm`, otherwise `none`. A list with another value or an empty list is invalid. |
| `running` | list | no | Sessions the worker still holds: `{session_id, lease_id, last_seq}` (`last_seq` is the highest `seq` the worker issued for the session). |
| `capacity` | integer, 1 or more | no | The maximum number of live sessions. Default 1. |
| `memory_mb` | integer, 1 or more | no | The memory budget of the worker in MiB. Default: `capacity` times the guest memory size of the API. |
| `arch` | string | no | The machine type, for example `x86_64` or `aarch64`. |
| `images` | list | no | Guest images on this host: `{id, version, digest, min_size}`. Entries without a string `id` are dropped. An absent list from a worker that accepts `microvm` means the images `default` and `browser` (only `default` on `aarch64`). |
| `version` | string | no | The ApiPi version of the worker. Used in a metric label only. |
| `features` | list of string | no | The protocol features the worker supports. Absent means the baseline set. See [Versioning and features](#versioning-and-features). |
| `capabilities` | object | no | Reserved. Ignored. |

#### `hello` (API to worker)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `hello`. |
| `ok` | boolean | yes | `true`. |
| `protocol` | integer | yes | `2`. |
| `lease_ttl_seconds` | number, above 0 | yes | The lease TTL the API enforces. |
| `heartbeat_seconds` | number, above 0 | yes | How often the worker MUST send `heartbeat`. One third of the lease TTL, at most 10. |
| `worker_id` | UUID | no | The worker id. The API always sends it. |
| `generation` | integer | no | Counts the registers of this worker. The API always sends it. |
| `connection_id` | string | no | A short id of this socket, for logs on both sides. |
| `sessions` | object | no | Maps a session id to the `last_seq` the API persisted. The worker MUST replay everything after it. With a non-empty `running` it lists the sessions whose lease matches the claim. With an empty `running` it lists every lease the API still assigns to this worker. |
| `store_check` | object | no | `{marker, nonce}`. Present only for the filesystem store. See below. |
| `revoke` | list | no | Leases the worker MUST drop: `{type: "lease.revoke", session_id, lease_id}`. A missing `lease_id` means the worker holds no lease for the session and MUST wipe its leftover workspace. |
| `ttl` | object | no | Maps a session id to `{idle_ttl_seconds, env_type, idle_since_epoch}`: the reaper TTL and the idle baseline. |
| `features` | list of string | no | The features the API supports. Absent means the baseline set. |

A worker MUST stop with an error when `lease_ttl_seconds` or
`heartbeat_seconds` is missing or not above 0. A worker MUST use these two
values and MUST NOT use a local setting for either one.

#### Shared store proof

With the filesystem store, `hello` carries `store_check: {marker, nonce}`.
The API has written a file named `marker` under the store root. The file
contains `nonce`. The worker reads the file from its own mount of the store
root and answers `{"type": "store.proof", "marker": ..., "nonce": ...}`. A
wrong proof closes the socket with the reason `shared_store_required`. A
worker that cannot read the file stops with the message
`filesystem store requires a shared path`. With the S3 store there is no
`store_check` and no proof.

#### `error` (API to worker)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `error`. |
| `ok` | boolean | yes | `false`. |
| `error` | string | yes | A short text for a person. |
| `code` | string | yes | A machine value. It equals the reason of the WebSocket close that follows. See [Errors and close codes](#errors-and-close-codes). |

A worker MUST decide on `code`. A frame from an API that predates `code` has
none, and the worker then matches the text in `error`. A first frame that
is neither `hello` nor a rejection is not fatal: the worker closes the
socket and reconnects.

### Control messages after the handshake

#### `heartbeat` (worker to API)

Every field except `type` is optional. A heartbeat renews every lease of the
worker. An invalid optional field is ignored and the lease is still renewed.

| Field | Type | Meaning |
| --- | --- | --- |
| `capacity` | integer, 1 or more | Maximum live sessions. |
| `memory_mb` | integer, 1 or more | Memory budget in MiB. |
| `run_mode` | string | The process backend. |
| `accepts` | list of string | Session kinds the worker runs. |
| `arch` | string | The machine type. |
| `image_store_version` | string | Identifies the image store. |
| `images` | list | The guest images, as in `register`. |
| `drain` | boolean | `true` marks a worker that takes no new sessions. `false` clears it. |

#### `lease.ack` (worker to API)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `lease.ack`. |
| `id` | UUID | yes | The `id` of the command, echoed exactly. |
| `lease_id` | UUID | yes | The lease of the command. |

The worker sends it as soon as it has the command. It does not say the
command finished. A retransmitted command is acked again. A worker does not
ack a command whose `op` it does not know.

#### `lease.release` (worker to API)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `lease.release`. |
| `session_id` | UUID | yes | The session the worker dropped. |
| `lease_id` | UUID | yes | The lease of the session. |

The worker sends it only after the API acked every envelope the worker
buffered for the session, or after 10 seconds, whichever comes first.

The worker releases the lease when the Pi process of a session stops,
for example after the idle TTL, at the memory limit, at drain, or at
shutdown. When a turn starts a new process for the same session,
because the turn needs a different configuration (`respawn`) or the old
process exited (`crash`), the worker reports the stop of the old
process and harvests its files, but it keeps the lease and sends no
`lease.release`, because the turn goes on under that lease.

A session is busy on the worker from the moment a `turn.start`,
`turn.continue`, or `sandbox.boot` arrives until that command has
finished. When the process of a busy session stops, for example at the
memory limit, after a crash, at the idle TTL, or because the
attachments of a message could not be copied into its guest
(`push_failed`), the worker reports the stop and harvests the files of
the process at once, but it sends `lease.release` only after the
command has finished, and only when the session has no live process
then. When a new process runs by then, the worker keeps the lease. The
worker also sends `lease.release` when a turn or a boot fails before
any process started, so a lease never stays without a process.

The API ignores a `lease.release` when it sent a `turn.start`,
`turn.continue`, or `sandbox.boot` on the same lease that the worker
had not acked before the release, in the order of the socket, or when
it sends such a command before it has handled the release. The worker
takes the lease back when that command arrives. Otherwise the API
clears the lease, and when the session still has a turn `in_progress`,
it ends that turn as `failed` with `turn_interrupted`: the release
comes after the envelopes of the session, so that turn can never
finish. A command for the session that the API wants to send while it
handles the release waits until the release has finished (up to 5
seconds), so the worker never gets a command for the lease it has just
released. A message or a `sandbox.boot` then goes out with a new
`lease_id`; a cancel, a stop, or a function result is answered as for a
session without a lease.

#### `lease.revoke` (API to worker)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `lease.revoke`. |
| `session_id` | UUID | yes | The session. |
| `lease_id` | UUID | yes | The lease that ended. |

The worker forgets the lease and the command ids of the session, tears down
the session (guest, Pi process, workspace), and drops the buffered envelopes
of the session.

#### `inventory` and `inventory.reply`

`inventory` has the field `sessions`: a list of `{session_id, lease_id,
last_seq}`. An entry without `lease_id` reports a workspace on disk that the
worker holds no lease for. `last_seq` defaults to 0.

`inventory.reply` has `revoke` (a list as in `hello`) and `ttl` (a map as in
`hello`). The API answers a leased session without revoke, and answers an
unleased workspace with a TTL while the session still exists and with a
revoke (no `lease_id`) once it is gone.

#### `sandbox.seen`

`{type: "sandbox.seen", session_ids: [UUID]}` lists the sessions whose
sandbox is live. The API ignores ids that are not leased to the connection.

#### `ack` (API to worker)

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `ack`. |
| `session_id` | UUID | yes | The session. |
| `last_seq` | integer, 0 or more | yes | Every durable envelope of the session with `seq` up to this value is persisted or rejected for a permanent reason. |

#### `search.request` and `search.reply`

`search.request` fields: `request_id` (UUID, chosen by the worker),
`session_id`, `turn_id`, `query` (string), and `max_results` (integer, 1 or
more, optional). The message carries no provider and no key.

`search.reply` fields: `session_id`, `request_id`, `ok` (boolean), `results`
(a list of `{title, url, snippet, published_date}`; `published_date` is a
string chosen by the provider and may be absent), `code`, and `message`.
When `ok` is `false`, `code` is one of `search_denied`, `search_unavailable`,
`search_timeout`, `search_failed`, `invalid_request`, and `message` is safe
to show to the model. The reply echoes `request_id` so the worker finds its
waiter.

#### `artifact.presign.reply` (API to worker)

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | string | `artifact.presign.reply`. |
| `session_id` | UUID | The session. |
| `request_id` | UUID | The `request_id` of the `artifact.presign` envelope. |
| `ok` | boolean | `false` when the API refused. Then `code` and `message` say why. |
| `unchanged` | boolean | `true` when the stored bytes already match the digest. The worker skips the upload. No `upload_id`, no `url`, no `path`. |
| `upload_id` | UUID | The upload slot. The worker echoes it in `artifact.completed`. |
| `artifact_id` | UUID | The id the stored artifact gets. |
| `url` | string | S3 only. A presigned PUT URL. |
| `headers` | object | S3 only. Headers the worker MUST send with the PUT. |
| `expires_at` | time | S3 only. When the URL stops working. |
| `path` | string | Filesystem only. The store-root relative path the worker MUST write. |
| `object_id` | string | The object key of the upload. |
| `file_id` | string | Never set. It carried the file id for the removed kind `input_image`. The field stays because removing a field needs a new protocol version. |
| `code`, `message` | string | The refusal. See [Errors and close codes](#errors-and-close-codes). |

## Command ops

A command is a control message from the API to the worker.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `command`. |
| `id` | UUID | yes | The command id. It is the idempotency key. |
| `session_id` | UUID | yes | The session. |
| `lease_id` | UUID | yes | The lease the command belongs to. |
| `op` | string | yes | One of `turn.start`, `turn.continue`, `turn.cancel`, `session.stop`, `sandbox.boot`. |
| `payload` | object | yes | The fields of the op, below. |

Rules for the worker:

* It acks every command it accepts with `lease.ack`, once, before it does the
  work. With the feature `session_stopped` it also acks `session.stop` on
  receipt. Without that feature it acks `session.stop` after the durable
  `session.stopped` envelope was acked.
* It runs a command once. The API sends a command again until it is acked,
  so the worker keeps recent command ids (at least 128 per session) for the
  life of the process. It acks a repeated id again and does not run it again.
  It forgets the ids of a session when the session is torn down, stopped, or
  revoked. A command that failed while it ran is not recorded as done, so a
  retransmit runs it again.
* It acks a command whose `payload.tenant_id` is missing or not a UUID and
  then rejects it: it does no work for it and releases the lease.
* It does not ack a command with an unknown `op`.
* It tracks a lease only for a command it accepted.

### Fields of every payload

| Field | Type | Meaning |
| --- | --- | --- |
| `tenant_id` | UUID | Required. The tenant of the session. |
| `request_id` | string | The id of the HTTP request that caused the command. The worker writes it in its logs and in `usage`. |
| `key_id` | string | The id of the API key. |
| `user_id`, `org_id` | string or `null` | Attribution. |
| `traceparent` | string | A W3C trace context. |
| `run_mode` | string | The kind the API placed the session as: `none` or `microvm`. The worker MUST NOT start a session of a kind it does not accept. It acks the command, reports the events `agent.session.error` and `agent.session.turn.failed`, and releases the lease. |
| `sandbox_image` | string | The guest image of a `microvm` session. A worker that does not have it reports a failed turn with code `image_unavailable`. |

### `turn.start`

| Field | Type | Meaning |
| --- | --- | --- |
| `context` | object | The [command context](#command-context). |
| `last_seq` | integer, 0 or more | The session cursor. See [Sequencing and delivery](#sequencing-and-delivery). |
| `text` | string | The user input as text. |
| `images` | list | Always an empty list. Images travel in `parts`. The field stays so that the payload does not change for older workers. |
| `parts` | list | The input in order: `{type: "input_text", text}`, [image references](#image-references), [file references](#input-file-references), and [workspace files](#workspace-files). |

#### Image references

An image part is a reference to a file in the store, like a
[file reference](#file-references) of the context. Image bytes never
travel in a command, so the size of an image does not count toward the
command size limit. The API stores every input image as a file before it
sends `turn.start`. The API sends image parts only to a worker that listed
the feature `image_refs`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `image`. |
| `file_id` | string | yes | The id of the file. The worker puts it in the user message item as `{type: "input_image", file_id}`. |
| `object_id` | string | yes | The object key of the file in the store. |
| `url` | string | S3 store | A presigned GET URL with a short TTL. |
| `local_path` | string | Filesystem store | The path relative to the store root. |
| `mime_type` | string | yes | The image type, for example `image/png`. |
| `size_bytes` | integer, 0 or more | no | The size of the file. |

The worker fetches the bytes the same way as a file reference, before it
reports the turn, and passes them to the model as images in the order of
`parts`. A worker that cannot fetch an image reports the turn as failed
with code `artifact_store`. A worker does not upload input images.

#### Input file references

A file part is one `input_file` of a session without a computer, as a
reference to a file in the store. Like an image, its bytes never travel
in a command. The API checks the type, the size, and (for text) the UTF-8
encoding of the file before it sends `turn.start`, and sends file parts
only to a worker that listed the feature `file_refs`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `file`. |
| `file_id` | string | yes | The id of the file. |
| `filename` | string | yes | The file name the model sees and the user item keeps. |
| `object_id` | string | yes | The object key of the file in the store. |
| `url` | string | S3 store | A presigned GET URL with a short TTL. |
| `local_path` | string | Filesystem store | The path relative to the store root. |
| `mime_type` | string | yes | The content type of the file, `application/octet-stream` when it has none. |
| `size_bytes` | integer, 0 or more | no | The size of the file. |
| `model_input` | string | yes | `text` or `image`: how the worker passes the file to the model. |

The worker fetches the bytes the same way as an image reference. For
`model_input` `image` it passes them to the model as an image, in the
order of the image parts. For `text` it decodes the bytes as UTF-8 and
puts the text in the prompt at the place of the part, inside a block
with the file name:

```
<file name="notes.md">
…content…
</file>
```

The prompt is then the `input_text` parts and these blocks in order,
joined by a newline. The worker puts the part in the user message item
as `{type: "input_file", file_id, filename}`. A worker that cannot fetch
a file reports the turn as failed with code `artifact_store`, and one
whose text file is not UTF-8 with code `invalid_request`.

#### Workspace files

In a session with a computer (`openai_hosted`) an `input_file` goes to
the workspace, not to the model. Its part is a `file` part with
`model_input` `workspace` and the `path` the API bound the file to in
the session, for example `attachments/report.xlsx`. It has no
`object_id`, `url`, or `local_path`: the bytes come from the
[`session_files`](#command-context) of the context, which lists the same
path. The API sends workspace parts, and a context with a non-empty
`session_files` list, only to a worker that listed the feature
`session_files`.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | yes | `file`. |
| `file_id` | string | yes | The id of the file. |
| `filename` | string | yes | The file name the user item keeps. |
| `mime_type` | string | yes | The content type of the file, `application/octet-stream` when it has none. |
| `size_bytes` | integer, 0 or more | no | The size of the file. |
| `model_input` | string | yes | `workspace`. |
| `path` | string | yes | The path of the file in the workspace. |

Before Pi starts the turn, the worker finds the reference of each
workspace part in `session_files` by its `path`, fetches its bytes, and
writes it into the session directory, replacing what is at that path.
The other entries of `session_files` are written only when their path
is missing (see [file references](#file-references)). On a `microvm`
guest that is already running, the worker also copies the files of the
workspace parts into the guest, because the guest sees the session
directory only at boot. The guest replaces a file at the same path. The
copy, from connect to the guest's answer, must end within 60 seconds.
Closing the vsock connection afterwards takes at most 5 more seconds,
also when the guest stopped reading. If the copy fails, the worker
reports `agent.session.environment.failed` and
`agent.session.error` with code `attachment_push_failed` (retryable)
without starting the turn, and then stops the guest with the reason
`push_failed`. The next turn boots a new guest from the session
directory. The worker puts one
line per workspace part in the prompt, at the place of the part, joined
to the other parts by a newline:

```
Attached: attachments/report.xlsx (xlsx, 240 KB)
```

The type is the extension of the path, or the content type when the
name has no extension. The worker puts the part in the user message item
as `{type: "input_file", file_id, filename, path}`.

### `turn.continue`

| Field | Type | Meaning |
| --- | --- | --- |
| `context` | object | The command context. Its `session.status` is `requires_action` and `session.required_actions` lists the open calls. |
| `last_seq` | integer, 0 or more | The session cursor. |
| `turn_id` | UUID | The turn that waits for the result. |
| `call_id` | string | The `call_id` of the function call. |
| `success` | boolean | Whether the tool call worked. |
| `output` | string | The tool output. |
| `error` | string | The tool error, when `success` is `false`. |

A worker MUST NOT continue the turn while other function calls of the same
turn are open. It removes the answered call from the open list, reports the
rest with a `session.status` envelope, and continues only when none is open.

### `turn.cancel`, `session.stop`

These two carry only the fields of every payload. `turn.cancel` aborts the
turn that runs in the session. A worker that holds no running turn does
nothing, and it does not create a lease for the session. `session.stop`
kills the guest, deletes the workspace files of the session, and reports the
durable `session.stopped` envelope.

### `sandbox.boot`

| Field | Type | Meaning |
| --- | --- | --- |
| `context` | object | The command context. |
| `last_seq` | integer, 0 or more | The session cursor. |

The worker starts the sandbox of a hosted session before a turn and reports
it with `sandbox.status` envelopes, and `lifecycle.start` once it is live.

### Command context

`turn.start`, `turn.continue`, and `sandbox.boot` carry `payload.context`,
built by the API for this one command. The worker keeps it in memory only.
File bytes never travel in a command.

| Field | Type | Meaning |
| --- | --- | --- |
| `session` | object | Required. See below. |
| `agent` | object | The resolved agent definition. |
| `model` | object | `{base_url, api_key}`: the model host override (or `null`) and the model key. This is the only place the key travels. |
| `mcp` | list | HTTP MCP servers: `{server_label, server_url, headers, allowed_tools}`. `headers` is a map of strings with the vault credentials applied. |
| `env_credentials` | list | Vault `environment_variable` credentials, decrypted: `{credential_id, secret_name, secret_value, allowed_hosts, git_username}`. Empty for `environment.type` `none`. See below. |
| `files` | list | Workspace files: `{path, object_id, url, local_path, size_bytes, content_type}`. |
| `session_files` | list | The files bound to the session with a workspace path, in the same shape as `files`: the attachments of earlier and current user messages. Empty for a session without a computer. A file deleted from the Files API is no longer in the list. |
| `skills` | list | Installed skills: `{skill_id, object_id, url, local_path}`. |
| `pi_session` | object | `{present, object_id, url, local_path}`: the saved Pi session for a cold restore. |

`session`: `environment` (object, for example `{type: "none"}` or
`{type: "openai_hosted", ...}`), `metadata` (object), `required_actions`
(list), `status` (string), `user_id`, `org_id`, `key_id`, `agent_id`, and
`idle_ttl_seconds` (number: the effective idle TTL of the session).

`agent`: `model`, `instructions`, `function_tools` (a list of function tool
definitions), `metadata`, `builtin_tools` (`on`, `off`, or `only`),
`codemode` (`on`, `off`, or `only`), `thinking`, and `web_search` (boolean).
`web_search` only says that the worker offers the search tool to the model.
It carries no provider, no key, and no domain list.

`env_credentials`: one object per environment credential of the
session's vaults, sorted by `secret_name`. `credential_id` (string),
`secret_name` (string, an environment variable name), `secret_value`
(string, the plain secret), `allowed_hosts` (list of lowercase
hostnames), and `git_username` (string or `null`, from the credential
metadata key `apipi.git_username`). The API has already checked the
session rules, so the names are unique and do not clash with
`environment.env`. The worker reads the list only when it starts a
sandbox: it gives the values to the egress gateway of that sandbox and
writes a fresh placeholder per credential into the guest environment.
A running sandbox keeps its snapshot, the same as for `mcp`. The
worker MUST NOT log `secret_value` and MUST NOT write it to the guest
or to the workspace drive. `redact_context` replaces it with `...`, and
the model repr leaves it out. The API sends a non-empty list only to a
worker that lists the feature `env_credentials`. A worker whose
isolation has no egress gateway (anything but `microvm`) fails the
sandbox start when the list is not empty.

#### File references

Each of `files`, `session_files`, `skills`, and `pi_session` is a reference. Exactly one of
two forms is used, depending on the store of the API.

| Store | Fields | What the worker does |
| --- | --- | --- |
| S3 | `url` | `GET` the presigned URL. The URL has a short TTL. The worker MUST NOT send credentials with it. `local_path` is absent. |
| Filesystem | `local_path` | Read the file at that path, relative to the store root, which the API and the worker mount at the same place. `url` is absent. The worker MUST NOT leave the store root: it rejects a path with `..` or an absolute path. |

`object_id` names the object in the store and is the same in both forms.
A worker stops reading a reference with `size_bytes` (files, session
files, image and file parts) after `size_bytes` bytes, and a skill
after `APIPI_MAX_FILE_BYTES`. A larger object is never held in memory
in full. An object larger than its limit, or a reference with
`size_bytes` whose object has another size, fails the turn with code
`artifact_store`.
`path` of a file is the path in the workspace. At the start of the turn
the worker fetches the bytes of a file of `files` or `session_files`
only when `path` does not exist in the session directory, and writes it
there. A file that exists is left as
it is and is not fetched. The worker then provisions the workspace and
installs the skills from their bytes.

## Events and items

The worker reports a turn with envelopes. The public session events
(`agent.session.*`) are what clients read on SSE, and the API stores them as
the worker reports them. A worker reports an event as an `event` envelope
with `payload: {type, data, turn_id}`. An event type that is not in the list
below is rejected.

### Public events

| Event `type` | `data` | When |
| --- | --- | --- |
| `agent.session.in_progress` | none | A turn starts. |
| `agent.session.turn.created` | `turn_id` | Right after `turn.status` `started`. |
| `agent.session.turn.in_progress` | `turn_id` | After `turn.created`. |
| `agent.session.turn.item.added` | `item_id`, `item_type`, `turn_id` for a stored item; `item_type`, `call_id`, `name`, `server_label`, `status`, `action` for a tool call | An item or a tool call starts. |
| `agent.session.turn.item.done` | `item_id`, `turn_id` for a stored item; `item_type`, `call_id`, `is_error`, `name`, `server_label`, `status`, `error` for a tool call | An item or a tool call ends. |
| `agent.session.turn.item.nested` | as a tool call, plus `parent_call_id` | A tool call inside another tool call (a codemode script). |
| `agent.session.turn.output_text.done` | `text`, `turn_id` | The model finished a text part. |
| `agent.session.turn.output_text.delta` | | Live only. Never sent as an `event`: the worker sends `delta.text` instead. The API rejects it as an `event`. |
| `agent.session.turn.thinking.started` | `item_id`, `content_index` | The model starts to reason. |
| `agent.session.turn.thinking.completed` | `item_id`, `content_index`, `duration_ms`, `reasoning_tokens`, `preview`, `preview_truncated` | The model stopped reasoning. |
| `agent.session.turn.compaction.started` | `reason` | Pi compacts the context. |
| `agent.session.turn.compaction.completed` | `reason`, `aborted`, `will_retry`, `error`, `tokens_before`, `tokens_after` | Compaction ended. |
| `agent.session.turn.retrying` | `attempt`, `max_attempts`, `delay_ms`, `code`, `failure_source`, `upstream_status` | The model host failed and the call is tried again. |
| `agent.session.turn.retry.completed` | `success`, `attempts` | The retry ended. |
| `agent.session.requires_action` | `turn_id`, `required_actions` | The turn waits for the client. |
| `agent.session.turn.completed` | `turn_id`, `usage` | The turn finished. |
| `agent.session.turn.failed` | `turn_id`, `message`, `code`, `failure_source`, `upstream_status`, `retryable`, optional `legacy_code`, `upstream_attempts` | The turn failed. |
| `agent.session.turn.cancelled` | `turn_id`, `failure_source` (`user`), `code` (`cancelled`), `reason` (`user`), `upstream_status`, `retryable` | The turn was cancelled. |
| `agent.session.error` | `message`, `code`, `detail_code`, `failure_source`, `upstream_status`, `retryable` | A session level error. |
| `agent.session.idle` | none | The session is idle. |
| `agent.session.failed` | none | The session failed outside a turn. |
| `agent.session.created` | | Accepted for completeness. |
| `agent.session.environment.pending`, `.connected`, `.disconnected`, `.failed` | | Sandbox phases. The API writes them from `sandbox.status`. A worker may send `environment.failed` with `{error, code}`. |

Every turn scoped event carries `data.turn_id`. A worker that reports tool
calls sets `item_type` to `command_execution`, `mcp_call`, or
`web_search_call`. A web search call has `status` (`in_progress`,
`completed`, `failed`) and `action: {type: "search", query}`.

### Items

An `item.added` envelope creates a stored item. Its payload has `item_id`
(UUID), `item_type`, `turn_id`, and `data`. The API accepts these
`item_type` values: `message`, `function_call`, `mcp_call`,
`mcp_list_tools`, `command_execution`, `web_search_call`. The reference
worker stores two kinds.

| `item_type` | `data` |
| --- | --- |
| `message` | `{role: "user" or "assistant", content: <string or list of parts>}`. A list holds `{type: "input_text", text}` and `{type: "input_image", file_id}`. |
| `function_call` | `{type: "function_call", call_id, name, arguments: <object>}`. |

The reference worker reports the tool call items of Pi only as events (`item_type` `command_execution`, `mcp_call`,
`web_search_call` in the event `data`). An item is always followed by the
two events `agent.session.turn.item.added` and `agent.session.turn.item.done`
with the same `item_id`. An `item.done` envelope may merge more `data` into
the item. The reference worker does not send one.

### Required order within a turn

The API depends on this order. A worker MUST emit the durable envelopes of
one turn in it, with consecutive `seq`.

1. `session.status` with `status` `in_progress` and `required_actions` `[]`.
2. Event `agent.session.in_progress`.
3. `turn.status` with `status` `started` (this creates the turn).
4. Events `agent.session.turn.created` and `agent.session.turn.in_progress`.
5. The user message item: `item.added`, then the events `item.added` and `item.done`.
6. The work of the model, in order: `event` envelopes for text, thinking,
   tool calls, and retries; items for function calls.
7. Then exactly one of three endings.
   * **Completed.** The assistant message item (three envelopes), then
     `turn.status` `completed`, then the uploads (`artifact.presign` and
     `artifact.completed` envelopes), then `usage`, then the event
     `agent.session.turn.completed`, then `session.status` `idle` with
     `required_actions` `[]`, then the event `agent.session.idle`.
   * **Requires action.** `session.status` `requires_action` with the open
     calls in `required_actions`, then the event
     `agent.session.requires_action`. The turn stays open. `turn.continue`
     resumes it at step 6 with the same `turn_id`.
   * **Failed or cancelled.** `turn.status` `failed` or `cancelled` with
     `code` and `message`, then `usage` (with `status` and, for failures,
     `failure`), then the event `agent.session.turn.failed` (followed by
     `agent.session.error`) or `agent.session.turn.cancelled`, then
     `session.status` `idle`, then the event `agent.session.idle`.

The API waits for `agent.session.requires_action` or `agent.session.failed`,
or for one of `turn.completed`, `turn.failed`, `turn.cancelled` and then
`agent.session.idle`. A worker that leaves out the last event of an ending
leaves the HTTP request of the client open until the turn timeout.

A command that fails before a turn exists (a wrong run mode, a missing
image, an invalid context) is reported as events `agent.session.error` and
`agent.session.turn.failed`, with `session.status` where it applies.

A durable envelope for a turn is accepted only while its turn is the running
turn, or the latest turn for terminal envelopes. An `item.added` for another
turn is rejected as `turn_mismatch`.

### Other durable envelope payloads

| Envelope `type` | Payload fields |
| --- | --- |
| `turn.status` | `turn_id`, `status` (`started`, `completed`, `failed`, `cancelled`), `code`, `message`. |
| `usage` | `turn_id`, `status`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_read_tokens`, `cache_write_tokens` (integers, 0 or more), `latency_ms`, `request_id`, `user_id`, `error_code`, `artifact_bytes`, `tool_names`, `tool_counts`, `mcp_names`, `mcp_counts`, and `failure` (`{message, code, failure_source, retryable, upstream_status, legacy_code, upstream_attempts}`). One per turn. |
| `session.status` | `status` (`idle`, `in_progress`, `requires_action`, `failed`), `required_actions` (list). At least one is set. |
| `error` | `code`, `message`, `turn_id`. The API stores an `agent.session.error` event. |
| `artifact.presign` | `request_id` (UUID), `kind` (`artifact`, `pi_session`), `filename`, `content_type`, `size` (integer, 1 or more), `sha256` (hex), `turn_id`. The kind `input_image` was sent by workers older than 0.15.0. It still parses, so that such a worker gets an answer, but the API refuses it (see [Removed upload kind](#removed-upload-kind)). |
| `artifact.completed` | `upload_id` (UUID from the reply), `path` (filesystem only), `name`, `size`, `sha256`, `turn_id`. |
| `session.stopped` | `reason` (`stop`). Completes `session.stop`. The API deletes the session blobs when it applies it. |
| `workspace.reaped` | `reason` (`idle`). A receipt that the idle reaper wiped a workspace. It changes nothing on the API. |
| `sandbox.status` | `status` (`starting`, `ready`, `stopped`), `reason`, `tenant_id`, `worker_id`, `image`, `image_version`, `size`, `run_mode`, `cause`, `cold`, `boot_ms`, `lock_wait_ms`, `setup_ms`, `live_ms`. |
| `lifecycle.start` | `cause`, `tenant_id`, `org_id`, `agent_id`, `user_id`, `key_id`, `environment_type`, `sandbox_size`, `sandbox_image`, `image_version`, `image_digest`, `run_mode`, `started_at`. The API takes identity from the session row and uses only the sandbox, run mode, and timing fields. |
| `lifecycle.stop` | `reason`, `live_ms`, `start_seq`, `started_at`. |
| `delta.text`, `delta.reasoning` | `turn_id`, `text`. Ephemeral. |

An upload runs like this. The worker sends `artifact.presign` and waits for
`artifact.presign.reply` (at most 60 seconds). With the S3 store it PUTs the
bytes to `url` with `headers` and no credentials. With the filesystem store
it writes the bytes to `path` under the store root. Then it sends
`artifact.completed` with the `upload_id`. The API checks the object: size
and checksum, and for the filesystem store the path. A reply with `unchanged`
ends the upload: the worker sends no `artifact.completed`. A worker MUST send
a presign only when the API listed the feature `presign`.

## State machines

### Connection

| State | Entered by | Leaves by |
| --- | --- | --- |
| Dialing | The worker starts or reconnects. | The WebSocket opens, or the dial fails and the worker backs off. |
| Registering | The worker sends `register`. | `hello` arrives (Ready), a rejection arrives (Closed), or 15 seconds pass without a frame (the worker closes the socket and reconnects). |
| Ready | `hello` is processed. The worker answers `store_check`, adopts `sessions`, applies `revoke` and `ttl`, and replays its outbox. | A socket close, a drain, or a protocol violation. |
| Draining | The worker sends `heartbeat` with `drain: true`. | The worker has no live session and the API acked every envelope. The worker exits. |
| Closed | Either peer closes. | The worker dials again, unless the close was a fatal rejection. |

The API sends nothing before `hello`. A connection is visible to placement
and commands only after `hello` is queued.

### Lease

A lease is owned by the API. It is stored on the session row.

| State | Entered by | Meaning |
| --- | --- | --- |
| Granted | The API places a session and sends `turn.start` or `sandbox.boot` with a new `lease_id`. | The command is not acked yet. |
| Active | The worker acks the command. | Every heartbeat, command ack, and committed batch renews it to now plus the lease TTL. |
| Released | The worker sends `lease.release` that crosses no `turn.start`, `turn.continue`, or `sandbox.boot` on the lease, or a `session.stop` completes. | The API cleared the lease and ended a turn that was still `in_progress` with `turn_interrupted`. |
| Expired | The lease TTL passes without a renewal. | The API clears the lease, stores `agent.session.error` with `worker_lease_expired`, fails the turn, and sends `lease.revoke`. The turn is not moved to another worker. |
| Orphaned | An inventory does not list a leased session and no command for it is unacked. | The API stores `agent.session.error` with `worker_orphaned` and clears the lease. |
| Revoked | `lease.revoke`, or a `revoke` entry in `hello` or `inventory.reply`. | The worker drops the session. |

### Turn (as the worker sees it)

| State | Entered by |
| --- | --- |
| Idle | The session exists on the worker. |
| Running | `turn.start` or `turn.continue` was accepted. The worker has sent `session.status` `in_progress`. |
| Requires action | The model called a function. The worker sent `requires_action`. |
| Done | The worker sent `turn.status` `completed`, `failed`, or `cancelled`, then `idle`. |

`turn.cancel` moves a running turn to Done as `cancelled`. A timeout of the
turn makes it Done as `failed` with `turn_timeout`.

### Command (as the API sees it)

| State | Entered by |
| --- | --- |
| Sent | The API wrote the frame. |
| Retransmitted | The worker reconnected, or 5 seconds passed since the last send while the worker is connected. The API sends the frame again, in order. |
| Acked | `lease.ack` arrived. The API removes the command. |
| Timed out | The lease TTL passed since the command was queued. The API clears the lease, stores `agent.session.error` with `worker_command_timeout`, fails the turn, and sends `lease.revoke`. |
| Dropped | The lease ended (released, expired, or revoked). |

### Outbox (as the worker sees it)

| State | Entered by |
| --- | --- |
| Appended | The worker appended a durable envelope and gave it the next `seq`. Over a bound it fails the turn instead. |
| Sent | The worker wrote it to the current socket. It sends each envelope once per socket. |
| Spooled | Optional. The worker wrote it to disk so that it survives a restart. |
| Acked | A cumulative `ack` with `last_seq` at or above its `seq` arrived. The worker deletes it. |

A reconnect resets "Sent", and the worker sends everything unacked once more,
in order.

## Sequencing and delivery

### Classes

| Class | Types | `seq` | Delivery |
| --- | --- | --- | --- |
| Durable | the 14 envelope types | One counter per session, assigned by the worker, starts at 1, increases by 1 for every durable envelope of the session, never repeats and never goes back. | Applied once, or rejected for a permanent reason. Kept in the outbox until acked. |
| Ephemeral | `delta.text`, `delta.reasoning` | A separate counter per session, starts at 1. The API does not check it. | At most once. Never persisted, never acked, never replayed. |
| Synchronous | `search.request`, `artifact.presign` and their replies | None (`search`), or the durable `seq` (`presign`). | See below. |
| Control | everything else | None. | Periodic or answered. The next one is the retry. |

### Rules

* The API ingests durable envelopes in batches and acks after the commit.
  The ack is cumulative: `ack{session_id, last_seq}` means every durable
  envelope of the session with a `seq` up to `last_seq` was applied, was
  already applied, or was rejected for a permanent reason. A permanent reject
  (not leased to this worker, a turn that does not match, an unknown event
  type, an invalid payload, an oversize envelope) is logged and counted and
  is acked past, so the worker does not send it again.
* A temporary failure (a deadlock, a timeout, a lost connection, a store
  throttle) is not acked, and neither is anything after it in the session.
  The API tries again, and after three tries it closes the socket
  (`ingest_failed`). The worker replays.
* A duplicate (same session and `seq`) is a no-op that is acked.
* An envelope of a type the API does not know is counted and not acked on
  its own. A later cumulative ack passes it. This is why a worker sends a new
  envelope type only when the API listed the matching feature.
* The `ack` is per session. A receiver does not assume that acks arrive for
  every `seq`: it takes the largest `last_seq` it has seen.
* `turn_id` appears in the envelope, in `payload.turn_id`, and for `event`
  envelopes also in `payload.data.turn_id`. A sender MUST set all that apply
  to the same value. The API reads them in that order and uses the first
  one.

### The cursor

The sequence number of a session is one counter for the life of the session,
not one per lease. The API stores the highest persisted value in
`sessions.worker_seq`. The worker learns it in three places.

1. On a **new lease**, every `turn.start`, `turn.continue`, and
   `sandbox.boot` carries `payload.last_seq`. Before the worker dispatches
   the command it continues its counter from this value, so the next
   envelope is `last_seq + 1`. It never moves the counter backwards, and it
   drops buffered envelopes at or below the value. A command without it is
   still dispatched.
2. On a **reconnect**, `hello.sessions` maps each session to the persisted
   value. The worker prunes envelopes up to it and replays the rest.
3. In an **inventory**, the worker reports its own highest `seq` for
   information.

Only the worker that holds the lease can move the cursor. A rejected envelope
from a worker that lost the lease is acked but does not move it.

### Reconnect and replay

After a reconnect the worker sends `register` with `running` (the leases it
holds and its highest `seq` per session), reads `hello`, and sends every
unacked durable envelope again, in order and once. The API replies with the
same `artifact.presign.reply` for a replayed presign, and sends it before the
ack. The API sends every unacked command of the leases of the worker again,
also for a lease the worker did not claim, because the worker may never have
read the command that granted it.

A session listed in `running` but missing in `hello.sessions` is not held by
the API: the worker drops it and tears it down. Buffered envelopes of
sessions without a lease claim stay: they are results from before a restart,
and the API acks them or rejects them as `not_leased`.

### Ordering

Frames of one direction arrive in the order they were written. The API
writes through one queue per connection. There is no order between a control
message and an envelope, with one rule that the worker enforces for itself:
it sends `lease.release` (and reports `session.stopped`) only after the API
acked the envelopes the worker had buffered for the session. Otherwise the
API would clear the lease and reject those envelopes as `not_leased`.

### Guarantees

| Message | Guarantee | Who retries |
| --- | --- | --- |
| Durable envelope | Applied once, or rejected for a permanent reason. | The worker, by replay. |
| Delta | At most once. | Nobody. |
| Command | At least once, in order per lease. Run once. | The API: on reconnect and every 5 seconds. |
| `session.stop` | Acked on receipt. Completed by `session.stopped`. If that does not arrive in 15 seconds, the API drops the lease anyway. | As a command. |
| `artifact.presign` | Always gets its reply, the same reply on replay. | The worker, by replay. |
| `search.request` | One reply, or an error for the model. | Nobody. A request is lost with the socket. |
| `lease.release` | Sent after the outbox was acked. If it never arrives, the lease expires. | The worker, after a reconnect. |
| `lease.revoke` | Sent when the API drops a lease. | `hello` and `inventory.reply` list it again. |
| `heartbeat`, `inventory`, `sandbox.seen` | Periodic. | The next one. |

## Timers and limits

"Set by" says who defines the value. A value from `hello` MUST be used as
sent. A constant of the protocol is the same for every peer. A value
"chosen by the worker" is the value of the reference worker. The API does not
need exactly that value, but the text says what breaks when a worker differs.

| Name | Value | Set by | When it runs out |
| --- | --- | --- | --- |
| Lease TTL | 30 s by default | API, sent in `hello.lease_ttl_seconds` | The lease is expired: the turn fails with `worker_lease_expired` and the API sends `lease.revoke`. |
| Heartbeat interval | Lease TTL divided by 3, at least 0.05 s and at most 10 s | API, sent in `hello.heartbeat_seconds` | A worker that sends later risks an expired lease. The worker sends it from a timer of its own, not when the socket is quiet. |
| WebSocket ping | Every 5 s, timeout 10 s | Worker | The worker closes the socket (`ping_timeout`) and reconnects. |
| Register timeout | 15 s | API | The API sends `error` with `register_timeout` and closes with `1008`. |
| `hello` timeout | 15 s | Worker | The worker closes the socket and reconnects (`hello_timeout`). |
| Inventory interval | 60 s | Worker | The API learns about orphaned or foreign leases late. A worker SHOULD send an inventory every 60 s and right after `hello`. |
| `sandbox.seen` interval | 5 s | Protocol constant | The API treats the sandbox as stale later. |
| Command retransmit | 5 s while connected, and on every reconnect | API | None. It repeats until the ack or the lease ends. |
| Command expiry | The lease TTL | API | `worker_command_timeout`: the API clears the lease, fails the turn, and revokes. |
| Commands per lease | 16 unacked | API | The API refuses a new command for the lease. |
| Release flush | 10 s | Worker | The worker sends `lease.release` anyway. |
| Stop completion | 15 s | API | The API drops the lease without `session.stopped` and logs it. |
| Search ack wait | 5 s | Worker | The worker does not send the request. The model gets a tool error. |
| Search reply wait | 30 s | Worker | The model gets a tool error. |
| Presign reply wait | 60 s | Worker | The upload fails. |
| Presign URL TTL | 15 min by default | API (`APIPI_PRESIGN_TTL`), sent in `expires_at` | The PUT is refused by the store. |
| Reconnect backoff | A random delay between 0 and min(10, 0.5 x 2^(n-1)) seconds after the n-th failure in a row (full jitter). The counter resets after a connection that stayed up 30 s. | Worker | None. |
| Write timeout | 10 s per frame, queue of 1024 frames | API | The API closes the socket (`write_timeout`). |
| Revoke send | 5 s | API | The revoke is not repeated until the next inventory. |
| Outbox | 10,000 messages and 64 MiB, and half of each per session (defaults) | Worker (settings) | The turn fails with `worker_outbox_full`. |
| Envelope size | 1,048,576 bytes | Protocol constant | The worker fails the turn with `worker_message_too_large` instead of sending. The API rejects it and acks past. |
| Command size | 262,144 bytes | Protocol constant | The API does not send the command and answers the HTTP request with `413` and `payload_too_large`, with a message that says the message and the session context are too large. Images are references and do not count. |
| Frame size | 4,194,304 bytes | API | The API closes the socket with code `1009`. |
| Delta text | At most 32,768 characters per envelope. The reference worker sends at most 4,000. | Protocol constant | The API rejects the delta. |
| Delta rate | 100 per second per session | Protocol constant | The API drops the excess and counts it. |
| Delta coalescing | About 40 ms | Worker | None. |
| Ingest batch | 100 envelopes or 50 ms | API | None. |
| Drain timeout | The idle TTL, 15 min by default | Worker (`--drain-timeout`) | The worker kills what is left, logs it, and exits with 1. |

## Errors and close codes

### Close codes

The API closes with a code and a reason. Both are visible to the worker. A
rejection that happens before `hello` is preceded by the error object whose
`code` equals the reason.

| Reason | Close code | When | What the worker does |
| --- | --- | --- | --- |
| `unauthorized` | 1008 | No bearer, a token without the `apipi_wk_` prefix, or an unknown token. | Stops. Retrying does not help. |
| `revoked` | 1008 | The token was revoked. The API also checks on every heartbeat. | Stops. |
| `unsupported_protocol` | 1008 | `register.protocol` is not 2. | Stops. |
| `register_required` | 1008 | The first message is not `register`. | Stops. |
| `invalid_register` | 1008 | The register is invalid, or its token was revoked in between. | Stops. |
| `token_bound` | 1008 | `register.id` differs from the worker the token is bound to. | Stops. |
| `shared_store_required` | 1008 | The store proof was wrong. This is the disconnect reason `protocol_violation` in the metrics. | Reconnects. A worker that cannot read the marker file stops by itself before it sends a proof. |
| `register_timeout` | 1008 | No first message in 15 s. | Reconnects. |
| `takeover` | 1000 | The same worker connected again, or a heartbeat came from an older generation. | Reconnects. |
| `write_timeout` | 1011 | A frame was not written in 10 s, or the send queue was full. | Reconnects and replays. |
| `ingest_failed` | 1011 | A batch still failed after three tries. | Reconnects and replays. |
| `ping_timeout` | 1011 | The keepalive ping got no answer. | Reconnects. |
| `clean` | 1000, 1001 | The worker closed the socket. | |
| (frame too large) | 1009 | A frame over 4,194,304 bytes. | Reconnects. |

A worker stops with a clear message on a rejection in the first six rows,
because retrying cannot help, and reconnects after any other loss. Only
`shared_store_required` can come after `hello`.

### What does not close a socket

A frame that is binary, not JSON, or not an object. A known `type` with
invalid fields. An unknown `type` or `op`. A database or store error while
handling one message. A heartbeat with an invalid optional field. A durable
envelope that fails validation: it is rejected and acked past.

### Reject reasons of envelopes

An envelope the API rejects is acked past, logged as `worker.event.rejected`,
and counted. The reasons are `not_leased`, `turn_mismatch`, `unknown_turn`,
`unknown_event`, `live_event`, `oversize`, `invalid_envelope`,
`not_implemented`, `ingest_error`, and the store codes of an upload.

### Reply error codes

| Message | Codes |
| --- | --- |
| `artifact.presign.reply` with `ok: false` | `artifact_store`, `artifact_too_large`, `workspace_too_large`, `invalid_envelope`, `ingest_error`. |
| `search.reply` with `ok: false` | `search_denied`, `search_unavailable`, `search_timeout`, `search_failed`, `invalid_request`. |

### Failure codes of a turn

A worker puts one of these codes in `turn.status`, `usage.failure.code`, and
the events `agent.session.turn.failed` and `agent.session.error`. Each code
has a fixed `failure_source` (`upstream`, `user`, or `internal`) and
`retryable` value, listed in [Failure codes](errors.md). The codes a worker
sets itself:

| Code | When |
| --- | --- |
| `worker_command_timeout` | Set by the API: a command was not acked within the lease TTL. |
| `worker_lease_expired` | Set by the API: the lease expired. |
| `worker_outbox_full` | The outbox bound was hit. |
| `worker_message_too_large` | One envelope is over 1,048,576 bytes. |
| `turn_timeout` | The turn ran longer than the turn timeout. |
| `cancelled` | The user cancelled the turn. |
| `artifact_store`, `workspace_too_large`, `artifact_too_large` | An upload failed or a quota was hit. |
| `spawn_failed`, `sandbox_boot_failed`, `pi_exited`, `pi_memory` | The sandbox or Pi failed. |
| `image_unavailable`, `placement` | The worker cannot take the session. |
| `invalid_request` | The command or its context is invalid. |
| `internal` | Anything else. A command that raised is answered with an `error` envelope with this code, unless the worker cancelled the command, for example on shutdown or a lost connection. |
| `upstream_*`, `context_length_exceeded`, `model_*` | Failures of the model host, as Pi reports them. |

## Versioning and features

The protocol version is the integer `2`, in `register.protocol` and
`hello.protocol`. The rules:

1. **A new field is always allowed.** A receiver ignores fields it does not
   know. A sender MAY add one without a feature.
2. **A new message type or command op needs a feature.** A peer sends it
   only when the other peer listed the feature in `register.features` or
   `hello.features`. A receiver that still gets an unknown `type` or `op`
   counts it and ignores it. It does not ack an unknown `op`, so the API
   sends it again and finally fails it.
3. **A change of behavior that an old peer cannot parse is a feature too.**
4. **Removing a field or changing what it means needs a new protocol
   version.** A `register` with another version is rejected as
   `unsupported_protocol`. There is no fallback.

A peer that sends no `features` list is a baseline peer. A list can contain
names the receiver does not know: it ignores them.

| Feature | What it adds | In the baseline |
| --- | --- | --- |
| `search` | `search.request` and `search.reply`. | Yes |
| `presign` | `artifact.presign`, `artifact.presign.reply`, and `artifact.completed`. | Yes |
| `lease_cursor` | `payload.last_seq` in `turn.start`, `turn.continue`, and `sandbox.boot`. | Yes |
| `session_stopped` | The worker acks `session.stop` on receipt. The durable `session.stopped` envelope completes it, and the API waits up to 15 seconds for it. | No |
| `image_refs` | `turn.start` carries input images as [image references](#image-references) in `parts`, and the worker no longer uploads them with the kind `input_image`, which the API refuses (see [Removed upload kind](#removed-upload-kind)). | No |
| `file_refs` | `turn.start` carries the `input_file` parts of a session without a computer as [file references](#input-file-references) in `parts`. | No |
| `session_files` | `turn.start` carries the `input_file` parts of a session with a computer as [workspace files](#workspace-files), and the context carries `session_files`. | No |
| `env_credentials` | The worker uses `context.env_credentials` when it starts a sandbox: placeholders in the guest environment, secret injection in the egress gateway, and the git credential helper. | No |

A worker does not wire web search when the API did not list `search`, and it
fails an upload with a clear error, without sending an envelope, when the API
did not list `presign`. The API refuses a command op that needs a feature the
worker did not list. It also refuses a `turn.start` with image parts for a
worker that did not list `image_refs`, a `turn.start` with file parts
for a worker that did not list `file_refs`, a `turn.start` with
workspace parts, or any command whose context has `session_files`, for a
worker that did not list `session_files`, and a `turn.start`,
`turn.continue`, or `sandbox.boot` whose `context.env_credentials` is
not empty for a worker that did not list `env_credentials`: the HTTP
request fails with `501` and code `unsupported_op`, and the message
names the feature. An older worker would ignore the unknown context
field and start the sandbox without the credentials, so the API never
sends it one.

**Upgrade order.** Upgrade the API first and the workers after it. A new API
tolerates old workers. An old worker rejects a command context with a field
it does not know. A release may add a field to a context or a payload only
after every worker and every API runs a version that ignores unknown fields.
`hello` MUST carry `lease_ttl_seconds` and `heartbeat_seconds` in every
version, because a worker cannot guess a safe heartbeat. While some workers
do not list `image_refs` yet, the API places a message with images on a
worker that lists it when one has room. A message with images that still
lands on an older worker fails with `501`. The same holds for `file_refs`
and a message with `input_file` parts, and for `session_files` and every
turn of a session with attachments. Text turns of other sessions are not
affected.

**Rollback order.** Roll back in the reverse order: the workers first, then
the API. A worker with `image_refs` accepts only image references and
rejects the old inline image parts of an older API, so an older API must not
run with newer workers while users send images.

### Removed upload kind

Workers older than 0.15.0 did not list `image_refs` and uploaded each input
image with an `artifact.presign` of kind `input_image`. The API presigned
that upload to the final key of the file. The kind is removed. A worker
that does not list `image_refs` never gets a turn with images, because the
API fails such a turn with `501` `unsupported_op`, so it has no reason to
send the kind. When an `artifact.presign` with the kind `input_image` still
arrives, the API answers it with `ok: false`, code `artifact_store`, and a
message that asks to upgrade the worker to 0.15.0 or later. It reserves no
upload slot and presigns no URL. The envelope is rejected with
`artifact_store` and acked past, and the worker fails the turn with that
code. An `artifact.completed` for an `input_image` upload slot that an
older API reserved is rejected with `artifact_store` too, and no file is
created.

This is not a new protocol version. The kind `input_image` stays in the
schema of `artifact.presign` and `file_id` stays in
`artifact.presign.reply`, so that an older worker's envelope still parses
and the refusal reaches it as a normal `ok: false` reply. A refusal with
`ok: false` was always a valid answer to a presign. The API never
presigns a PUT to a `files` or `skills` key for a worker.

## Security rules for a worker

* The worker holds the command context in memory only. It does not write the
  context, the model key, or MCP headers to disk, to a log, to a metric, or
  to an error message. It logs a summary only: the operation, the
  environment type, the model name, the MCP labels, and the number of files
  and skills.
* The worker never logs the bearer token, the model key, MCP headers, or a
  presigned URL with its query string.
* The worker holds no database credentials and no object-store credentials,
  and no search provider name or key. It reads and writes files only through
  the references of the context, the image references of `turn.start`, and
  the replies of `artifact.presign.reply`.
* The hosts a worker may call are these and no others: the API socket; the
  model host in `context.model.base_url` (in a sandbox, through the broker of
  the session, so the key does not enter the guest); the HTTP MCP servers in
  `context.mcp`; the presigned URLs of the context and of image references
  (GET) and of presign replies (PUT); and, for an operator, the image store
  that holds guest images.
* The worker applies an SSRF guard to every MCP `server_url`. It resolves the
  host name and refuses loopback, private (RFC 1918), link-local, carrier-grade
  NAT, multicast, and reserved addresses, including cloud metadata addresses
  such as `169.254.169.254`, whether the host is a name or a literal address.
  An operator allow list (`APIPI_MCP_ALLOW_HOSTS`) can admit a private target.
  A sandbox filters its own outbound traffic to the same private ranges.
* The worker treats the API as the only source of commands, and the context as
  data that can contain untrusted text. It does not run a command whose
  `lease_id` it does not hold, and it refuses a `local_path` or a workspace
  path that leaves its root.
* The API treats the worker as untrusted. It checks the lease of every
  envelope, the tenant of every search request, and the object of every
  upload. A worker cannot widen what the API allows.

## What a worker implements

A worker in another language implements the wire, the lease and outbox
rules, and a harness.

| Part | Required for any worker | Specific to the ApiPi worker (Python) |
| --- | --- | --- |
| Socket client, register, `hello`, heartbeat, reconnect, ping | Yes | `websockets` |
| Commands: dedupe, ack, the five ops | Yes | |
| Command context and file references | Yes | |
| Outbox: `seq`, send once per socket, cumulative ack, replay, bounds | Yes | Optional disk spool of JSON Lines |
| Events and items in the required order | Yes | The mapping from Pi events in `worker/pi/map.py` |
| Presign uploads, search, inventory, `sandbox.seen`, `lease.release` | Yes | |
| The agent harness that runs a model and tools | A harness of its own | Pi over RPC, one process per session |
| The sandbox | A sandbox of its own, or none | Firecracker microVMs, the guest broker, the image store |
| Idle reaper, workspace wipe | Yes, driven by `hello.ttl` and `inventory.reply.ttl` | `reap_loop`, `reap_workspace_loop` |
| Metrics and logging | Recommended | Prometheus series, named log events |

A foreign worker needs its own harness. The wire stays the same. It reports
the same events, in the same order, from whatever its harness does.

## JSON Schema and golden transcripts

### Schema

`docs/worker-protocol/schema/` holds one JSON Schema (draft 2020-12) for
every message, generated from the models in `src/apipi/protocol/`. The file
`index.json` lists them and says which direction each belongs to.

| File | Describes |
| --- | --- |
| `<type>.json` | The control message with that `type`: `register`, `hello`, `error`, `heartbeat`, and so on. |
| `command.json`, `command.<op>.json` | A command, and a command of one op with its payload and context. |
| `envelope.json`, `envelope.<type>.json` | An envelope, and an envelope of one type with its payload. |
| `context.json` | The command context. |

A frame is checked against a schema chosen like this: a frame with `v` is
`envelope.<type>`; a frame with `type` `command` is `command.<op>`; any other
frame is `<type>`. The schemas allow unknown fields and are stricter than
the Python parser in two places: ids are lowercase hyphenated UUIDs and
`started_at` and `expires_at` are RFC 3339 UTC times.

The files are generated. Do not edit them. After a change to a model run
`uv run python scripts/gen_worker_schema.py`. A test fails when the
committed files differ from the models. Another test checks that every frame
the Python API and worker send in the test suite validates against the
schema.

### Golden transcripts

`tests/fixtures/worker-protocol/*.jsonl` holds one transcript per scenario.
The frames were recorded from the Python API and worker and then normalized.
A transcript is a JSON Lines file: one JSON object per line. The first line
is the header. Every other line is a step.

**Header.**

| Field | Meaning |
| --- | --- |
| `fixture` | The name. |
| `format` | `1`. |
| `title`, `description` | Text for people. |
| `modes` | Optional. A list of `api` and `worker`. The default is both. See below. |
| `api` | Optional settings for a real API: `artifact_store` (`s3`) and `search` (`true`). |
| `worker` | Optional settings for a real worker: `inventory_seconds`. |
| `worker_exit` | Optional. The exit status the real worker ends with. |

**Steps.**

| Step | Fields | Meaning |
| --- | --- | --- |
| frame | `from` (`worker` or `api`), `frame`, optional `optional`, `lost`, `comment` | One frame, written by `from`. |
| `{"step": "connect"}` | | A new WebSocket connection begins. The first step is always one. |
| `{"step": "disconnect"}` | | The socket drops without a close handshake. |
| `{"step": "close"}` | `code`, `reason` | The API closes the socket. |
| `{"step": "action"}` | `for` (`api` or `worker`), `name`, `args` | Something that only the real peer can do. A player that plays that peer ignores it, and the other player does it. |
| `{"step": "check"}` | `for`, `name` | A state of the real API to check. |

**Players.** One player plays one peer and checks the other.

* In **api mode** the player is the worker. It sends the frames with `from`
  `worker` and checks every frame the real API sends. It performs the
  actions for `api`.
* In **worker mode** the player is the API. It sends the frames with `from`
  `api` and checks every frame the real worker sends. It performs the actions
  for `worker`.

A worker in another language runs worker mode: the player is a fake API, the
worker under test connects to it, and the runner sets up the model so that
the worker does what the transcript says (for example a model that calls a
function). The Python tests that do this are `tests/unit/test_conformance_worker.py`
(real worker) and `tests/api/test_conformance_api.py` (real API). The shared
matching code is `tests/support/conformance.py` and is about 250 lines.

**Matching.** A received frame matches a step when every key of the expected
frame is present in the received frame and its value matches. Extra keys in
the received frame are ignored. Lists match in length and element by
element. A string value of the form `<kind>` or `<kind:name>` is a
placeholder.

| Placeholder | Received frame | Frame to send |
| --- | --- | --- |
| `<any>` | Any value. | Not allowed. |
| `<string>` | A string. | `"string"`. |
| `<int>` | An integer. | `1`. |
| `<number>` | A number. | `1.0`. |
| `<bool>` | A boolean. | `true`. |
| `<uuid>` | A lowercase hyphenated UUID. | A new UUID. |
| `<ts>` | A time as in the data types. | The current time. |
| `<absent>` | The key must not be present. | The key is left out. |
| `<store_check>` | An object with `marker` and `nonce`. | A new store check: the player writes the marker file under the store root. |
| `<var:name>` | The value bound to `name`. | The value bound to `name`. |

`<string:name>` and `<uuid:name>` also bind the value to `name` on first sight
(`name` then holds it for the rest of the transcript and across connections)
and later occurrences have to be equal. `<store_check>` binds `marker` and
`nonce`. An object key can be `<var:name>`. A sent placeholder with a name
that is not bound yet is generated and bound. A `<var:name>` for a name that
is not bound is an error.

**Order.**

* Steps are played in file order. A player that plays a peer sends its frame
  when it reaches the step, and it waits for a frame of the other peer when
  it reaches that step.
* Frames of one direction are matched in order. While a player waits for a
  frame, it skips a received frame of type `delta.text`, `delta.reasoning`,
  `heartbeat`, or `sandbox.seen` that the step does not name, because a real
  peer sends them at times that depend on timing.
* A cumulative `ack` step matches a received `ack` of the same session whose
  `last_seq` is at least the value of the step. A received `ack` with a
  smaller value is skipped.
* A step with `optional` may be absent. A player that expects it ignores it
  (deltas are skipped by the rule above). A player that sends it does send it.
* A step with `lost` is a frame the sender wrote and the receiver never got
  because the socket dropped. A player that plays the sender does not send it.
  A player that plays the receiver still expects it from the real peer and
  then discards it.

**Actions.** The Python players know these names.

| Name | For | What it does |
| --- | --- | --- |
| `create_session` | `api` | Creates an agent and a session through HTTP and binds `session` and `tenant`. `args.tools` lists `echo` (a function tool) or `web_search`. |
| `start_turn` | `api` | Posts the message `args.text`. |
| `submit_tool_result` | `api` | Posts the tool result for `args.call_id` with `args.output`. |
| `cancel_turn` | `api` | Posts the cancel event. |
| `stop_session` | `api` | Deletes the session, which sends `session.stop`. |
| `seed_cursor` | `api` | Sets the persisted `last_seq` of the session to `args.last_seq`. |
| `lease_session` | `api` | Leases the session to the registered worker and binds `lease`. |
| `expire_lease` | `api` | Lets the lease expire and runs the reaper. |
| `store_bytes` | `api` | Writes `args.text` where the presign reply said (S3 object or store-root path). |
| `script_model` | `worker` | Sets what the model does: `function_calls`, `hold` (wait for cancel), or `search` (`query`, `max_results`). |
| `create_workspace` | `worker` | Creates a workspace directory for the session that no lease covers. |
| `upload` | `worker` | Starts an upload (`kind`, `filename`, `content_type`, `text`). |
| `drain` | `worker` | Starts a drain, as `SIGTERM` does. |

**Checks.** `worker_draining` (the API marked the worker as draining) and
`lease_cleared` (the session has no lease).

**Scenarios.**

| File | Shows |
| --- | --- |
| `register-hello` | Register, hello, store proof. |
| `register-rejected` | The error object, for a wrong protocol and for a first message that is not `register`. api mode only. |
| `turn-text` | A turn with text, a delta, usage, and one cumulative ack. |
| `turn-continue` | A function call, `requires_action`, `turn.continue`, and the cursor continuing. |
| `cancel` | `turn.cancel` and the cancelled ending. |
| `new-lease` | `payload.last_seq` on a fresh worker. |
| `reconnect-replay` | `lost` frames, `hello.sessions`, and the replay. |
| `presign-filesystem`, `presign-s3` | Upload with `artifact.presign`, the reply, and `artifact.completed`. |
| `search` | `search.request` after the ack wait, and `search.reply`. |
| `session-stop` | `session.stop` acked on receipt and completed by `session.stopped`. |
| `revoke` | The lease expires and the API sends `lease.revoke`. |
| `drain` | A heartbeat with `drain`, and the exit. |
| `inventory-unleased` | An inventory with a workspace that has no lease, and the reaper TTL in the reply. |

## Known differences

These are places where the code does not match the rules above, or where the
rule is stricter than what the Python code checks. Each is a bug or a
decision that is still open. They are tracked in
[#499](https://github.com/GEKI-AI/apipi/issues/499) and
[#500](https://github.com/GEKI-AI/apipi/issues/500).

| What | Rule | What the code does |
| --- | --- | --- |
| Integers | No string coercion. | The Python receivers accept a numeric string where an integer is expected. The schema rejects it. |
| `seq` | Starts at 1. | The API also accepts 0. |
| `payload.tenant_id` of a command | Required. | The command builder of the hub does not add it to a payload that lacks it. The API's HTTP paths always set it, and the worker drops a command without it. The schema leaves it optional. |
| Times | RFC 3339 UTC with `Z`. | The API now writes `expires_at` this way. `idle_since_epoch` is a number of seconds, and `published_date` is a string from the search provider. A receiver accepts the offset form. |
| Timers from `hello` | Every value that the two sides must agree on comes from `hello`. | Only the lease TTL and the heartbeat interval do. The inventory interval, the `sandbox.seen` interval, and the release flush are constants of the worker. |
| Envelope `v` | An envelope is a frame with `v`. | The API treats only `v` equal to 2 as an envelope. Another value is counted as an unknown type. |
