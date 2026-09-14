# Environments

An environment is where file and shell tools run. That choice is
independent of [run mode](architecture.md), which is where Pi itself
runs. When the computer is local, Pi and the files share the same
isolation boundary. A remote runner is valid with `none` and
`microvm`. That is the only supported split.

## Types

| `environment.type` | When |
| --- | --- |
| `openai_hosted` | Default. Session directory next to Pi. |
| `hosted` | Alias for `openai_hosted`. Stored and returned as `openai_hosted`. |
| `none` | No filesystem, no shell. |
| `self_hosted` | External runner. Tools go over a socket. |

`openai_hosted` is OpenAI's field name for a local session directory.
It is **not** OpenAI's cloud VM. `hosted` means the same folder. You
can override the type per session. If you omit `environment` on
create, the gateway uses `openai_hosted`.

Search and browser are not environments. Attach them as MCP. See
[tools](tools.md).

## Local directory (`openai_hosted`)

One directory per session. Pi and this folder share the same
[run mode](run-modes.md) isolation. There is no mode that puts Pi in
one sandbox and the local files in another. The path is
`{APIPI_SESSIONS_DIR}/{tenant_id}/{session_id}`. When
`APIPI_SESSIONS_DIR` is unset, that root is `.apipi/sessions` under the
gateway's working directory.

In isolation `none` this is a folder on the host. It is not a security
boundary. In `microvm`, that folder is packed into a workspace drive
at boot, unpacked onto a guest tmpfs, and is the guest cwd. Before the
guest exits, those writes are pulled back to the host folder.

Session rows live in the store. Environment files are the computer.
The `openai_hosted` directory lasts across Pi stop until
`APIPI_WORKSPACE_TTL` or session delete. That directory is also
bounded by `APIPI_MAX_WORKSPACE_BYTES` (default 1GiB). Artifact
metadata is in the store; artifact bytes are copied to the gateway host
when a turn completes, up to `APIPI_MAX_ARTIFACT_BYTES` (default
512MiB) per session. See [run modes](run-modes.md#storage) and
[config](config.md).

There is no runner socket. The directory is created when the session is
created. File tools (read, write, edit, bash) run against that folder.

## `none`

No computer. Pi still runs the loop. Function tools and MCP still
work. There is no session directory and no shell.

## `self_hosted`

Pi stays in the run mode (`none` or `microvm`). Production
SaaS and enterprise still run that Pi under `microvm`. The computer
is elsewhere. You must sandbox the runner. The gateway does not nest
the remote runner in a microvm.

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
`/v1/runners` resource. One key is one workspace. That socket must
reach the same gateway process that created the session. See
[multiple nodes](scale.md).

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

A runnable example that attaches a local directory as that computer is
`examples/self_hosted_runner.py`. It speaks this protocol. Pass the
one-time `key` and `environment_id` from session create in the
environment, not in the file. Setup is in `examples/README.md`.

## Skills

`environment.capability_directories` lists paths on this computer that
contain skill directories (`SKILL.md`). They are discovered when the
session starts. See [tools](tools.md).

Stdio MCP (for example Playwright) follows Pi, not the remote runner.
HTTP MCP is reached from the gateway and handed to Pi.
