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
2. Response has `environment_id` and a short-lived key
3. `environment.pending` -> runner connects -> `environment.connected`
4. `read` / `write` / `edit` / `bash` go over the socket, not through
   the jail or guest

| Verb | Job |
| --- | --- |
| `hello` | Auth, capabilities |
| `exec` | Command in workspace cwd |
| `read` / `write` / `edit` / `list` | Files |
| `artifact` | Publish an output |
| `ping` | Keepalive |
| `close` | Shutdown |

No `/v1/runners` resource. One key = one workspace.

If nothing connects, the session still runs without file tools.

## Skills

`environment.capability_directories`: paths on this computer that contain
skill directories (`SKILL.md`). Discovered when the session starts.

Search and browser are not environments. Attach MCP ([tools](tools.md)).
Stdio MCP (Playwright) follows Pi, not the remote runner.
