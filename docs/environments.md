# Environments

An environment is where file and shell tools run. That choice is
independent of [isolation](isolation.md) (run mode), which is where Pi
itself runs. When the computer is local, Pi and the files share the same
isolation boundary. A remote runner is valid with `none` and
`microvm`. That is the only supported split.

## Types

| `environment.type` | When |
| --- | --- |
| `openai_hosted` | Default. Session directory next to Pi. |
| `hosted` | Alias for `openai_hosted`. Stored and returned as `openai_hosted`. |
| `none` | No filesystem, no shell. |
| `self_hosted` | External runner. Tools go over a socket. Not an ApiPi sandbox worker. |

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

In isolation `none` this is a folder on the host. In `microvm`, that
folder is packed into a workspace drive
at boot and unpacked onto a guest tmpfs at `/workspace`. Files under
`outputs/` are harvested when a turn completes. Scratch files do not
survive sandbox stop.

Session rows live in the store. The `openai_hosted` workspace is
ephemeral: after `APIPI_SANDBOX_TTL_OPENAI_HOSTED` (default 1 hour)
with no activity, Pi stops and the directory is deleted. Transcript,
published artifacts, and the harness session cache stay. The next turn
creates an empty `/workspace`, re-applies skills, packages, setup
commands, files, env, and network policy, and reloads the cached
session file so Pi continues the
conversation. Published files are not copied back into `/workspace`.
The directory is bounded by `APIPI_MAX_WORKSPACE_BYTES` (default 1GiB).
Artifact bytes are copied to the gateway host when a turn completes,
up to `APIPI_MAX_ARTIFACT_BYTES` (default 512MiB) per session. See
[run modes](run-modes.md#storage) and [config](config.md).

There is no runner socket. The directory is created when the session is
created. File tools (read, write, edit, bash) run against that folder.

### Sandbox size

Session create may include `environment.sandbox_size` with value `S`,
`M`, or `L`. That field is an ApiPi extension. Official OpenAI clients
that reject unknown environment keys can set
`metadata["apipi.sandbox_size"]` instead. Other `metadata` keys stay
opaque tags; the `apipi.` prefix is reserved for gateway scheduling
and resources.

Resolution, highest wins:

1. `environment.sandbox_size`
2. Session create `metadata["apipi.sandbox_size"]`
3. Agent `metadata["apipi.sandbox_size"]`
4. Gateway `APIPI_SANDBOX_DEFAULT_SIZE` / `[sandbox].default_size`
   (shipped default `S`)

The resolved size is stored on the session `environment` and is fixed
for the life of the live guest. Updating session metadata later does
not resize or reimage an already chosen size.

| Size | Guest RAM | Rootfs | When |
| --- | --- | --- | --- |
| `S` | `[sandbox.resources].mem_mib` (512) | `default` | Pi and light tools |
| `M` | `APIPI_SANDBOX_M_MEM_MIB` (1024) | `default` | Heavier non-browser work |
| `L` | `APIPI_SANDBOX_L_MEM_MIB` (2048) | `browser` | Chromium in the guest plus Playwright MCP tools (unless you already attached them). Install the browser rootfs. |

Isolation `none` accepts the field and does not apply RAM or rootfs.
Isolation `microvm` applies both, including when `environment.type` is
`none` (Pi still runs in a guest). Each live lease consumes that
size's RAM against worker `memory_mb` and still counts as one session.

On `microvm`, size `L` injects a Playwright stdio MCP server
(`npx @playwright/mcp`, headless, isolated, system Chromium at
`/usr/bin/chromium-browser`) so the browser just works without
`examples/playwright.yaml`. If the agent already has a Playwright MCP
tool (`server_label` `playwright` or the same package), that tool is
kept and nothing is duplicated. Set `[sandbox.browser].auto_playwright
= false` to keep L RAM and rootfs but attach MCP yourself. A Playwright
process that exits immediately fails the guest instead of booting L
without browser tools. The platform prompt mentions Chromium only when
those tools are attached.

### Packages, files, env, network, and setup commands

Session create may include `environment.packages`,
`environment.setup_commands`, `environment.env`, inline
`environment.files`, and `environment.network` on `openai_hosted`.
Those fields are stored on the session. Prep runs before the first
agent turn that needs the computer:

1. Write inline `files` into the session directory. Paths use the same
   `/workspace` and `/tmp/workspace` mapping as setup `cwd`. Other
   absolute paths, `..` escapes, and writes under `.apipi/` are
   rejected. `data` is standard base64. The decoded total must fit
   `APIPI_MAX_WORKSPACE_BYTES`.
2. Apply `env` (string keys and values) to that session's Pi process
   and to prep. Reserved names are rejected: `PATH`, `HOME`, `USER`,
   `SHELL`, `PWD`, `LD_LIBRARY_PATH`, `LD_PRELOAD`, `OPENAI_API_KEY`,
   `OPENAI_BASE_URL`, `DATABASE_URL`, `PI_CODING_AGENT_DIR`, and any
   name starting with `APIPI_`, `CODEX_`, or `PI_`.
3. Install `packages.python`, then `packages.system`, then
   `packages.npm`.
4. Run `setup_commands` in order. Each item is an object with
   `command` and optional `cwd`. `cwd` defaults to the session
   directory. Absolute OpenAI paths `/workspace` and `/tmp/workspace`
   map to that directory. Other absolute paths are rejected.

`network.access` is `enabled`, `disabled`, or `restricted`.
`restricted` requires `allowed_domains` (1–100 exact hostnames).
`enabled` allows outbound traffic unless the process-wide TAP
allowlist is on; then the gateway list still wins. `disabled` blocks
guest TAP egress (DNS and the host broker on the TAP subnet still
work). `restricted` allows only those hostnames, plus package
registries when `packages` is set so install can run. A session cannot
add a host that `[sandbox.network]` forbids. Model and HTTP MCP calls
go through the host broker, so they still work when TAP is locked.

After a sandbox TTL wipe, the next turn recreates `/workspace` and
re-applies the stored files, env, packages, setup commands, and
network policy.

Isolation `none` runs that script in the session directory on the host
(`uv pip` or `python3 -m pip`, `apk` or `apt-get` if present, `npm`).
Missing tools fail the session. Isolation `none` cannot enforce TAP
policy: `disabled` and `restricted` fail the environment with a clear
error; `enabled` is a no-op. Isolation `microvm` packs the same
script into the guest and runs it after unpack, before Pi, in the same
guest, and applies `network` on that guest TAP. When the optional TAP
allowlist is on, install hosts (PyPI, npm, Alpine) are added for that
session if the matching package list is set.

A nonzero exit emits `agent.session.environment.failed` and
`agent.session.failed`. Pi does not start. Successful prep is visible
in the workspace before the turn. `none` and `self_hosted` environment
types reject packages, setup commands, env, and files. `self_hosted`
also rejects `network`. Environment type `none` ignores `network`.

## `none`

No computer. Pi still runs the loop. Function tools and MCP still
work. There is no session directory and no shell.

## `self_hosted`

Pi stays in the run mode (`none` or `microvm`). Production
SaaS and enterprise still run that Pi under `microvm`. The computer
is elsewhere. You must sandbox the runner. The gateway does not nest
the remote runner in a microvm. This socket is not the trusted
[sandbox worker](workers.md) protocol.

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
