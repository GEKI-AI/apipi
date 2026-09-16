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
           |  in-process, or a worker lease
           v
      Pi  (+ stdio MCP)           none | microvm
           |
           +-- local files        next to Pi (/workspace in a guest)
           +-- or remote env      self_hosted runner
           +-- HTTP MCP           e.g. Tavily
           +-- model host         OPENAI_BASE_URL
```

A turn is one model loop. The client posts a message. The API
authenticates the bearer, loads the session, and asks execution to
run. Combined `apipi serve` runs Pi in that process.
`apipi serve --api-only` leases a [worker](worker-concepts.md). Pi
runs in [isolation](isolation.md) (`none` or a Firecracker guest).
Public events are written to the store, then SSE. Token deltas are
live only and are not stored.

Two knobs:

| Knob | What it controls |
| --- | --- |
| **Run mode** | Where Pi and stdio MCP run (`APIPI_RUN_MODE`) |
| **Environment** | Where file and shell tools run (`environment.type`) |

They combine. `self_hosted` does not replace a microVM around Pi. The
API stays on the host.

Agents, sessions, the computer, and artifacts below are the pieces the
durable store keeps (and, for files, disk or object storage). One
process uses SQLite. Several processes share Postgres.

## Agents

An agent is saved configuration: model, instructions, tools, and
metadata. You create them; a fresh database has none.

You create agents with `POST /v1/agents`. They live in the store until
you delete them. A session may pass `agent_id` or an inline `agent`.
Inline config is used for that session only. It is not saved unless
you `POST /v1/agents`. Inline model and instructions are kept on the
session for follow-up turns. Saved agents keep reading the agent row.

When instructions are set, the gateway appends them to Pi's system
prompt so the model follows them. Empty or omitted instructions leave
Pi's default prompt unchanged.

Changing a saved agent later does not rewrite history on existing
sessions.

## Sessions

A session is one conversation. The durable store holds the session
row, the append-only event log, turns, and items. Token deltas are
live SSE only and are not stored. That transcript is the source of
truth. Pi's on-disk files are a cache.

Create a session with `POST /v1/agents/sessions`. A non-empty `input`
starts the first turn. Follow-up messages go to
`POST /v1/agents/sessions/{session_id}/events`. Status is `idle`,
`in_progress`, `requires_action`, or `failed`.

When a `none` or `self_hosted` session is idle for `APIPI_IDLE_TTL`
(default 15 minutes), the gateway kills that Pi process to free RAM.
An `openai_hosted` computer lasts until
`APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour): Pi stops and the
workspace is deleted. The session row stays. The next message starts
Pi again, rebuilds `/workspace` from stored config (skills, packages,
setup commands), and continues from the event log.
`GET /v1/agents/sessions/{id}/export` returns the transcript as JSON.
A session export is enough to leave.

`DELETE` removes the session for that tenant, including the workspace
directory, artifact metadata, and stored bytes.

## The computer and files

Where file and shell tools run is independent of [run mode](run-modes.md),
which is where Pi itself runs.

| `environment.type` | Files |
| --- | --- |
| `openai_hosted` (default) | A local directory next to Pi. OpenAI's field name; not OpenAI's cloud. `hosted` is the same. |
| `none` | No filesystem and no shell. |
| `self_hosted` | An external runner. Tools go over a WebSocket. |

On `openai_hosted`, the path is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. In a microVM the guest
cwd is `/workspace`. Read, write, edit, and bash run against that
folder. After `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour) with
no activity, Pi stops and the directory is deleted. The transcript and
published artifacts stay. The next turn rebuilds `/workspace` from
stored config. That directory is bounded by
`APIPI_MAX_WORKSPACE_BYTES` (default 1GiB).

On `self_hosted`, files stay on the runner. The gateway also copies
`artifacts/` and `outputs/` from the runner on turn complete and on Pi
stop if the socket is up. On `none`, there is no computer.

A crash before publish can lose unpublished files under `artifacts/`
or `outputs/`.

## Artifacts

An artifact is a named output the API can fetch after a turn
completes. Metadata is in the store, including `turn_id` when the file
was published at turn complete, plus `key_id` and byte size. Bytes
live in the configured artifact store: local files under
`{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{key_id}/{session_id}/{id}`,
or S3-compatible object storage with the same key layout.
`GET .../artifacts/{id}/content` reads that store in every run mode.
`410` if nothing was published. `DELETE` removes the metadata and the
file. The live file on the computer is unchanged. Artifacts last until
you delete the artifact or the session. The host store for one session
is bounded by `APIPI_MAX_ARTIFACT_BYTES` (default 512MiB). Publishing
over that cap emits `agent.session.error` with code
`artifact_too_large` and does not write the extra bytes.

Ask the agent to write under `artifacts/` or `outputs/` if you need
the file after the workspace expires. Copies are immutable. A later
turn that writes the same path publishes another artifact. Harvest on
Pi stop is a safety net for files written after the last completed
turn.

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
`artifacts/` and `outputs/` are published as immutable artifacts.
Those copies remain downloadable after the sandbox expires. The next
turn recopies skills and re-runs packages and setup commands into a
fresh workspace.

OpenAI's `self_hosted` files stay with your provider and are not
published through their Artifacts API. Ours stay on the runner the
same way, except we also copy `artifacts/` and `outputs/` from the
runner on turn complete and on Pi stop if the socket is up.

Session conversation state is similar: both keep turns and items so
you can continue later. OpenAI stores that on their side. ApiPi stores
it in your store. Export is how you take the thread with you.

`packages` and `setup_commands` on `openai_hosted` install dependencies
and run prep commands before the first turn. Other OpenAI environment
fields such as `files`, `env`, or `network` return an error
(`not_implemented`).
