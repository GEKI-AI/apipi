# Environments

Where file and shell tools run. Independent of [run mode](architecture.md).
A remote runner works with `host`, `jail`, and `microvm`.

## Types

| `environment.type` | When |
| --- | --- |
| `openai_hosted` | Default. Session directory next to Pi. |
| `none` | No filesystem, no shell. |
| `self_hosted` | External runner. Tools go over a socket. |

`openai_hosted` is **not** OpenAI's cloud VM. Override per session or in
server config.

## Local directory (`openai_hosted`)

One directory per session.

- `host`: folder on the host. Not a security boundary.
- `jail`: bind-mounted into the namespace jail.
- `microvm`: guest workspace.

No socket. Starts with the session.

## `self_hosted`

Pi stays in the run mode (`host` / `jail` / `microvm`). The computer is
elsewhere.

1. Session created with `self_hosted`
2. Response has `environment_id` and a one-time `key`
3. `environment.pending` -> runner connects -> `environment.connected`
4. `read` / `write` / `edit` / `bash` go over the socket, not through
   the jail or guest

Create JSON includes `environment.id`, top-level `environment_id`, and
`key` (this response only). `required_actions` may include
`environment_connection` until the runner is connected. Status stays
`idle` so turns can run. If nothing connects, file tools stay off.

The runner opens `/v1/environments/{environment_id}` as a WebSocket and
sends `hello` with the key. Wrong id or key is not found. No
`/v1/runners` resource. One key = one workspace.

Messages are JSON objects. `hello` is first:

```json
{"type": "hello", "key": "..."}
```

Gateway replies `{"type": "hello", "ok": true}`. Later requests have
`id`. Replies: `{"id": "...", "ok": true, ...}` or
`{"id": "...", "ok": false, "error": "..."}`.

| Verb | Direction | Job |
| --- | --- |
| `hello` | runner → gateway | Auth, capabilities |
| `exec` | gateway → runner | Command in workspace cwd |
| `read` / `write` / `edit` / `list` | gateway → runner | Files |
| `artifact` | gateway → runner | Publish an output |
| `ping` | either | Keepalive |
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

Artifact bytes are `read` while the socket is up. `410` if it is gone
or disconnected.

## Skills

`environment.capability_directories`: paths on this computer that contain
skill directories (`SKILL.md`). Discovered when the session starts.

Search and browser are not environments. Attach MCP ([tools](tools.md)).
Stdio MCP (Playwright) follows Pi, not the remote runner.
