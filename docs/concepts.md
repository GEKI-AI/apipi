# How it fits together

ApiPi is an HTTP gateway compatible with the OpenAI Agents API. Your
app talks to the gateway. The gateway talks to Pi. Pi talks to your
model URL. Isolation and the computer are separate choices: where Pi
runs, and where files run.

```
  OpenAI SDK / your app
           |
           |  bearer
           v
      ApiPi HTTP API              never inside a guest
      store (SQLite or Postgres)
           |
           |  worker lease
           v
      Pi  (+ MCP)           none | microvm
           |
           +-- local files        next to Pi (/workspace in a guest)
           +-- HTTP MCP           e.g. Tavily
           +-- model host         OPENAI_BASE_URL
```

A hosted computer is not paused. Idle expiry stops Pi and deletes the workspace. The session stays. The next turn builds a new computer and reloads the transcript. See [environments](environments.md).

A turn is one model loop. The client posts a message. The API
authenticates the bearer, loads the session, and asks execution to
run. `apipi serve` is always the API: it leases a
[worker](worker-concepts.md), and Pi runs there. Pi runs in [isolation](isolation.md) (`none` or a Firecracker guest).
Public events are written to the store, then SSE. Token deltas are
live only and are not stored. A thinking preview is stored. The full
thinking text is not.

Two knobs:

| Knob | What it controls |
| --- | --- |
| **Run mode** | Where Pi and MCP run (`APIPI_RUN_MODE`) |
| **Environment** | Where file and shell tools run (`environment.type`) |

Pi and the computer always share one isolation boundary, and there is no split. The
API stays on the host.

Agents, sessions, the computer, and artifacts below are the pieces the
durable store keeps (and, for files, disk or object storage). One
process uses SQLite. Several processes share Postgres.

## Agents

An agent is saved configuration: model, instructions, tools,
metadata, and session defaults. You create them; a fresh database has
none. Session defaults are the environment and vaults that a new
session inherits unless the request overrides them. A vault holds API
keys and tokens that the session can use without the model or the
sandbox seeing them (see [Vaults and credentials](vaults.md)). A
template is a
zip of that configuration you can store, download, and use to create
a new agent in the same tenant. See [API](api.md#agents) and
[agent templates](agent-templates.md).

You create agents with `POST /v1/agents`. They live in the store until
you delete them. A session may pass `agent_id` or an inline `agent`.
Inline config is used for that session only. It is not saved unless
you `POST /v1/agents`. Inline model and instructions are kept on the
session for follow-up turns. Saved agents keep reading the agent row.

Pi always receives a gateway platform prompt after its harness default,
unless an operator or caller replaces that default. Composition order
is: Pi's default, or a replacement system prompt when one is set; then
the main platform prompt (the shipped file for the actual
computer, or an operator override); then optional additional platform
text; then, for a hosted microvm only, a size line and an optional
network line; then `agent.instructions`. The extension replaces only
Pi's intro line with an identity sentence that still names Pi as the
harness. Computer sessions say they run in a sandbox.
`environment.type=none` sessions do not. The name is `[pi].platform_name`. The rest of Pi's prompt
stays. `environment.type=none` sessions do not get sandbox or `/workspace` text in the
main prompt. The hosted prompt does not state how long the sandbox
stays up. It says the sandbox stops after some idle time, that
user-provided files under `inputs/` and files the user attached to a
message under `attachments/` are restored in their original version
after a restart, that edits to them last until then, and that other
workspace files, including `outputs/`, do not survive a restart.
Empty or omitted agent instructions skip only that last block. The platform prompt is operator config, not a
transcript item. A replacement system prompt is
`metadata["apipi.system_prompt"]` on the session, then the agent, then
`[pi].system_prompt`. That replacement keeps the appended platform
blocks, instructions, context files, and skills. It removes Pi's tool
list and all tool guidelines, including MCP guidance.
The tools stay callable. See [config](config.md#pi).

Changing a saved agent later does not rewrite history on existing
sessions.

## Sessions

A session is one conversation. The durable store holds the session
row, the append-only event log, turns, and items. Token deltas are
live SSE only and are not stored. Thinking events store a short
preview, not the full text. That transcript is the source of
truth. Pi's on-disk files are a cache.

Create a session with `POST /v1/agents/sessions`. A non-empty `input`
starts the first turn. Follow-up messages go to
`POST /v1/agents/sessions/{session_id}/events`. Status is `idle`,
`in_progress`, `requires_action`, or `failed`.

When a `none` session is idle for `APIPI_IDLE_TTL`
(default 15 minutes), the worker that holds Pi kills it to free RAM.
That follows the environment type, not the run mode. `apipi worker`
owns the idle reap; `apipi serve` does not. An
`openai_hosted` computer lasts until
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour): one timer stops Pi
and deletes the workspace together. There is no separate guest timeout.
A session or agent `idle_ttl` replaces that default for that session.
The session row stays. The next message starts
Pi again, rebuilds `/workspace` from stored config (skills, packages,
setup commands), reloads the cached harness session file so the model
keeps the conversation, and continues the event log.
`GET /v1/apipi/sessions/{id}/export` returns the transcript as JSON.
A session export is enough to leave.

`DELETE` removes the session for that tenant, including the workspace
directory, artifact metadata, stored bytes, and the harness session
cache.

## The computer and files

Where file and shell tools run is independent of [run mode](run-modes.md),
which is where Pi itself runs.

| `environment.type` | Files |
| --- | --- |
| `openai_hosted` (default) | A local directory next to Pi. OpenAI's field name; not OpenAI's cloud. `hosted` is the same. |
| `none` | No filesystem and no shell. |
| `self_hosted` | Currently not supported (`not_implemented`; may return on worker protocol v2). |

On `openai_hosted`, the path is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. In a microVM the guest
cwd is `/workspace`. Read, write, edit, and bash run against that
folder. After `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with
no activity, Pi stops and the directory is deleted. The transcript,
published artifacts, and the harness session cache stay. The next turn
rebuilds `/workspace` from stored config and reloads that cache so Pi
continues the conversation. Scratch files and published artifact bytes
are not copied back into `/workspace`. That directory is bounded by
`APIPI_MAX_WORKSPACE_BYTES` (default 1GiB).

The gateway also copies
`outputs/` from the hosted computer on turn complete and on Pi stop. On `none`, there is no computer.

A crash before publish can lose unpublished files under `outputs/`.

## Artifacts

An artifact is a named output the API can fetch after a turn
completes. Metadata is in the store, including `turn_id` when the file
was published at turn complete, plus `key_id` and byte size. Bytes
live in the configured object store: local files under
`{APIPI_LOCAL_STORE_DIR}/.artifacts/{tenant_id}/{key_id}/{session_id}/{id}`,
or S3-compatible object storage with the same key layout. Hosted file
and skill uploads share that backend (local or S3) under separate key
namespaces; see [config](config.md).
`GET .../artifacts/{id}/content` reads that store in every run mode.
`410` if nothing was published. `DELETE` removes the metadata and the
file. The live file on the computer is unchanged. Artifacts last until
you delete the artifact or the session. The host store for one session
is bounded by `APIPI_MAX_ARTIFACT_BYTES` (default 512MiB). Publishing
over that cap emits `agent.session.error` with code
`artifact_too_large` and does not write the extra bytes.

Ask the agent to write under `outputs/` if you need the file after the
workspace expires. Copies are immutable. A later turn that writes the
same path publishes another artifact. Harvest on Pi stop is a safety
net for files written after the last completed turn. Existing stores
may still list rows whose path starts with `artifacts/`. Those remain
readable. New publishes use `outputs/`.

## Compared with OpenAI

OpenAI's Agents API uses the same agent and session shapes. The
computer is different.

On OpenAI, `openai_hosted` is a cloud Linux sandbox. The working
directory is `/workspace`. Files in that sandbox last across turns
until the sandbox goes idle for about an hour. Files under
`/workspace/outputs` are published as immutable artifacts when a turn
completes. Those copies remain downloadable after the sandbox expires.

On ApiPi, `openai_hosted` is a folder on your machine next to Pi. The
working directory in a microVM guest is `/workspace`. Files last
across turns until the sandbox is idle for
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour). Then Pi stops and
scratch files are deleted. When a turn completes, files under
`outputs/` are published as immutable artifacts. Those copies remain
downloadable after the sandbox expires. The next
turn recopies skills and re-runs packages and setup commands into a
fresh workspace.

Provider-hosted files stay with your provider and are not
published through their Artifacts API. Ours stay in the hosted workspace
the same way, except we also copy `outputs/` into the artifact store on turn
complete and on Pi stop.

Session conversation state is similar: both keep turns and items so
you can continue later. OpenAI stores that on their side. ApiPi stores
it in your store. Export is how you take the thread with you.

`packages` and `setup_commands` on `openai_hosted` install dependencies
and run prep commands before the first turn. Other OpenAI environment
fields such as `files`, `env`, or `network` return an error
(`not_implemented`).
