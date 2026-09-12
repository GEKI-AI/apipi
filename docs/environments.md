# Environments

An environment is where file and shell tools run. That choice is
independent of [run mode](architecture.md), which is where Pi itself
runs. A remote runner is valid with `host`, and it will be valid with
`jail` and `microvm` when those modes exist. Today only `host` starts;
`jail` and `microvm` exit.

## Types

| `environment.type` | When |
| --- | --- |
| `openai_hosted` | Default. Session directory next to Pi. |
| `none` | No filesystem, no shell. |
| `self_hosted` | External runner. Tools go over a socket. |

`openai_hosted` is OpenAI's field name for a local session directory.
It is **not** OpenAI's cloud VM. You can override the type per session.
If you omit `environment` on create, the gateway uses `openai_hosted`.

Search and browser are not environments. Attach them as MCP. See
[tools](tools.md).

## Local directory (`openai_hosted`)

One directory per session. The path is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. When
`APIPI_SESSIONS_DIR` is unset, that root is `.apipi/sessions` under the
gateway's working directory.

In `host` (the only implemented run mode) this is a folder on the host.
It is not a security boundary. When `jail` exists, the plan is to
bind-mount that folder into the namespace jail. When `microvm` exists,
the plan is to use it as the guest workspace.

There is no runner socket. The directory is created when the session is
created. File tools (read, write, edit, bash) run against that folder
while Pi is alive.

## `none`

No computer. Pi still runs the loop. Function tools and MCP still
work. There is no session directory and no shell.

## `self_hosted`

Pi stays in the run mode (`host` today; `jail` / `microvm` later). The
computer is elsewhere.

1. Create the session with `environment.type` `self_hosted`.
2. The create response includes `environment.id` on the environment
   object, a top-level `environment_id`, and a one-time `key`. The key
   is not stored in plaintext and is not returned again.
3. The gateway emits `environment.pending`. When the runner connects,
   it emits `environment.connected`. If the socket drops, it emits
   `environment.disconnected`.
4. `read` / `write` / `edit` / `bash` go over the socket, not through
   the Pi process's local filesystem.

`required_actions` may include `environment_connection` until the
runner is connected. Session status stays `idle` so turns that do not
need files can still run. If nothing connects, file tools stay off.

The runner opens `/v1/environments/{environment_id}` as a WebSocket and
sends `hello` with the key. Wrong id or key is not found. There is no
`/v1/runners` resource. One key is one workspace.

Messages are JSON objects. `hello` is first:

```json
{"type": "hello", "key": "..."}
```

The gateway replies `{"type": "hello", "ok": true}`. Later requests
have `id`. Replies: `{"id": "...", "ok": true, ...}` or
`{"id": "...", "ok": false, "error": "..."}`.

| Verb | Direction | Job |
| --- | --- |
| `hello` | runner → gateway | Auth, capabilities |
| `exec` | gateway → runner | Command in workspace cwd |
| `read` / `write` / `edit` / `list` | gateway → runner | Files |
| `artifact` | gateway → runner | Publish an output |
| `ping` | either | Keepalive (`pong` back) |
| `close` | either | Shutdown |

```json
{"id": "...", "type": "exec", "command": "ls"}
{"id": "...", "type": "read", "path": "a.txt"}
{"id": "...", "type": "write", "path": "a.txt", "content": "..."}
{"id": "...", "type": "edit", "path": "a.txt", "old_text": "...", "new_text": "..."}
{"id": "...", "type": "list", "path": "."}
{"id": "...", "type": "artifact", "path": "out.bin"}
{"id": "...", "type": "ping"}
{"id": "...", "type": "close"}
```

Artifact bytes are `read` while the socket is up. `410` if the file is
gone or the runner is disconnected.

## Skills

`environment.capability_directories` lists paths on this computer that
contain skill directories (`SKILL.md`). They are discovered when the
session starts. See [tools](tools.md).

Stdio MCP (for example Playwright) follows Pi, not the remote runner.
HTTP MCP is reached from the gateway and handed to Pi.
