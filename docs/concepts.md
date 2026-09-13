# Concepts

This page is how the gateway holds work: agents, sessions, the
computer, and artifacts. The HTTP routes are in [API](api.md). How to
install the process is in [Install](install.md). How to call it is in
[Using the API](using.md).

## Agents

An agent is saved configuration: model, instructions, tools, and
metadata. It is not a running process. There is no built-in agent on a
fresh install.

You create agents with `POST /v1/agents`. They live in Postgres until
you delete them. A session may pass `agent_id` or an inline `agent`.
Inline config is used for that session only. It is not saved unless
you `POST /v1/agents`.

Changing a saved agent later does not rewrite history on existing
sessions.

## Sessions

A session is one conversation. Postgres holds the session row, the
append-only event log, turns, and items. That transcript is the
source of truth. Pi's on-disk files are a cache.

Create a session with `POST /v1/agents/sessions`. A non-empty `input`
starts the first turn. Follow-up messages go to
`POST /v1/agents/sessions/{session_id}/events`. Status is `idle`,
`in_progress`, `requires_action`, or `failed`.

When Pi is idle for `APIPI_IDLE_TTL` (default 15 minutes), the gateway
kills that process. The session row stays. The next message starts Pi
again and continues from the event log. `GET /v1/agents/sessions/{id}/export`
returns the transcript as JSON. A session export is enough to leave.

`DELETE` removes the session for that tenant, including artifact
metadata and stored bytes.

## The computer and files

Where file and shell tools run is independent of [run mode](run-modes.md),
which is where Pi itself runs.

| `environment.type` | Files |
| --- | --- |
| `openai_hosted` (default) | A local directory next to Pi. OpenAI's field name; not OpenAI's cloud. |
| `none` | No filesystem and no shell. |
| `self_hosted` | An external runner. Tools go over a WebSocket. |

On `openai_hosted`, the path is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. While Pi is alive,
read, write, edit, and bash run against that folder. When Pi stops,
the gateway copies files under `artifacts/` into the host artifact
store, then deletes the workspace. The folder is scratch. A later
spawn is a fresh directory. Skills listed in
`capability_directories` are copied in again.

On `self_hosted`, files stay on the runner. On `none`, there is no
computer.

A crash before Pi stop can lose unpublished files under `artifacts/`.

## Artifacts

An artifact is a named output the API can fetch after Pi stops.
Metadata is in Postgres. Bytes live on the gateway host under
`{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{session_id}/{id}`.
`GET .../artifacts/{id}/content` reads that store in every run mode.
`410` if nothing was published. `DELETE` removes the metadata and the
file. Artifacts last until you delete the artifact or the session.

Ask the agent to write under `artifacts/` if you need the file after
the process is gone.

## Compared with OpenAI

OpenAI's Agents API uses the same agent and session shapes. The
computer is different.

On OpenAI, `openai_hosted` is a cloud Linux sandbox. The working
directory is `/workspace`. Files in that sandbox last across turns
until the sandbox goes idle for about an hour. Files under
`/workspace/outputs` are published as immutable artifacts when a turn
completes. Those copies remain downloadable after the sandbox expires.

On ApiPi, `openai_hosted` is a folder on your machine next to Pi. The
workspace lasts across turns only while that Pi process is alive
(idle TTL, default 15 minutes). When Pi stops, the workspace is
deleted. Only files harvested from `artifacts/` remain on the gateway.
We do not keep a live sandbox after the process exits. We do not
publish outputs at the end of every turn; we copy `artifacts/` when Pi
stops.

OpenAI's `self_hosted` files stay with your provider and are not
published through their Artifacts API. Ours stay on the runner the
same way, except we also try to copy `artifacts/` from the runner when
Pi stops if the socket is up.

Session conversation state is similar: both keep turns and items so
you can continue later. OpenAI stores that on their side. ApiPi stores
it in your Postgres. Export is how you take the thread with you.

Do not send OpenAI-only environment fields such as `packages`,
`network`, or `files` on create. Unknown fields return an error.
