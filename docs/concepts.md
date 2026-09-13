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
kills that process to free RAM. The session row stays. The next
message starts Pi again and continues from the event log.
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
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. Read, write, edit, and
bash run against that folder. Killing idle Pi does not delete it. The
next spawn uses the same directory. After `APIPI_WORKSPACE_TTL`
(default 1 hour) with no session activity, and only if Pi is already
gone, the gateway deletes that directory. The transcript and published
artifacts stay. Skills listed in `capability_directories` are copied
in again on the next spawn into a fresh workspace.

On `self_hosted`, files stay on the runner. The gateway also copies
`artifacts/` and `outputs/` from the runner on turn complete and on Pi
stop if the socket is up. On `none`, there is no computer.

A crash before publish can lose unpublished files under `artifacts/`
or `outputs/`.

## Artifacts

An artifact is a named output the API can fetch after a turn
completes. Metadata is in Postgres, including `turn_id` when the file
was published at turn complete. Bytes live on the gateway host under
`{APIPI_SESSIONS_DIR}/.artifacts/{tenant_id}/{session_id}/{id}`.
`GET .../artifacts/{id}/content` reads that store in every run mode.
`410` if nothing was published. `DELETE` removes the metadata and the
file. The live file on the computer is unchanged. Artifacts last until
you delete the artifact or the session.

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

On ApiPi, `openai_hosted` is a folder on your machine next to Pi. Files
last across turns after Pi stops, until `APIPI_WORKSPACE_TTL` (default
1 hour) with no session activity. `APIPI_IDLE_TTL` (default 15 minutes)
only kills the process. When a turn completes, files under
`artifacts/` and `outputs/` are published as immutable artifacts.
Those copies remain downloadable after the workspace expires.

OpenAI's `self_hosted` files stay with your provider and are not
published through their Artifacts API. Ours stay on the runner the
same way, except we also copy `artifacts/` and `outputs/` from the
runner on turn complete and on Pi stop if the socket is up.

Session conversation state is similar: both keep turns and items so
you can continue later. OpenAI stores that on their side. ApiPi stores
it in your Postgres. Export is how you take the thread with you.

Do not send OpenAI-only environment fields such as `packages`,
`network`, or `files` on create. Unknown fields return an error.
