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
published artifacts, and the harness session cache stay (the session
row stores a `file://` or `s3://` URI for that cache). The next turn
creates an empty `/workspace`, re-applies skills, packages, setup
commands, files, env, and network policy, and reloads the cached
session file so Pi continues the
conversation. Published files are not copied back into `/workspace`.
There is no pause. A TTL stop kills Pi and deletes the workspace. The public state is `stopped` with `reason: idle`. A client may label that "paused" or "sleeping", but must not imply that files survive.

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
opaque tags. The `apipi.` prefix is reserved. The gateway reads
`apipi.sandbox_size` (this page) and `apipi.session_kind` (placement).
Extenders may set `apipi.actor_type`, `apipi.schedule_id`, and
`apipi.source`. The gateway stores those keys and does not schedule
from them. The full list is in
[reserved metadata](extending.md#reserved-metadata).

Resolution, highest wins:

1. `environment.sandbox_size` on the session
2. Session create `metadata["apipi.sandbox_size"]`
3. Agent `session_defaults.environment.sandbox_size`
4. Agent `metadata["apipi.sandbox_size"]` (deprecated alias)
5. Gateway `APIPI_SANDBOX_DEFAULT_SIZE` / `[sandbox].default_size`
   (shipped default `S`)

The resolved size is stored on the session `environment` and is fixed
for the life of the live guest. Updating session metadata later does
not resize or reimage an already chosen size.

### Sandbox image

`environment.sandbox_image` chooses the guest image by id. It is
separate from size. Size is RAM. Official clients can set
`metadata["apipi.sandbox_image"]` instead. Resolution, highest wins:

1. `environment.sandbox_image` on the session
2. Session create `metadata["apipi.sandbox_image"]`
3. Agent `session_defaults.environment.sandbox_image`
4. Agent `metadata["apipi.sandbox_image"]` (deprecated alias)
5. Size `L` selects `browser`. Other sizes fall through.
6. `APIPI_SANDBOX_DEFAULT_IMAGE` / `[sandbox].default_image` (shipped
   default `default`)

Pin both on an org bot so later sessions start in that guest. Prefer
`session_defaults`. The metadata keys are still accepted and are copied
into `session_defaults` on write.

```json
{
  "name": "org-bot",
  "session_defaults": {
    "environment": {"type": "openai_hosted", "sandbox_image": "browser", "sandbox_size": "M"}
  }
}
```

Agent create and update reject a bad size, an unknown image, or a size
below that image's minimum, whether you set the field on
`session_defaults` or the metadata alias. Worker availability is still
checked when a session is created.

The resolved id is stored on the session `environment` as
`sandbox_image`. A later metadata update does not reimage a live guest.
An id must match `^[a-z0-9][a-z0-9-]{0,31}$`. An unknown id is `400`.
Each image has a minimum size. `browser` needs `M` or larger. A smaller
size is `400`. Isolation `none` stores the field and does not apply it.

Playwright MCP is injected when the resolved image is `browser` and
auto-inject is on, not because the size is `L`. The shipped default
still maps `L` to `browser`, so existing `L` sessions keep the tools.

| Size | Guest RAM | Rootfs | When |
| --- | --- | --- | --- |
| `S` | `[sandbox.resources].mem_mib` (512) | `default` | Pi and light tools |
| `M` | `APIPI_SANDBOX_M_MEM_MIB` (1024) | `default` | Heavier non-browser work |
| `L` | `APIPI_SANDBOX_L_MEM_MIB` (2048) | `browser` | Chromium in the guest, 2 vCPUs by default (`APIPI_SANDBOX_L_VCPUS`). Playwright MCP tools are injected when auto-inject is on, and named in the prompt only after they register. Install the browser rootfs. |

Isolation `none` accepts the field and does not apply RAM or rootfs.
Isolation `microvm` applies both, including when `environment.type` is
`none` (Pi still runs in a guest). Each live lease consumes that
size's RAM against worker `memory_mb` and still counts as one session.

On `microvm`, image `browser` starts the Playwright MCP server that
the browser rootfs already contains. The command is `node` and
`/opt/apipi/playwright-mcp/node_modules/@playwright/mcp/cli.js`, with
the same headless Chromium flags. It does not run `npx`. Size `L`
still selects `browser` when the image is omitted. Install that
rootfs with `apipi install --microvm --image browser`. An older
browser rootfs without that file cannot attach. Attach waits at most
15 seconds, then the turn continues without those tools. The worker
logs `pi.extension_error` with the server label, the phase, and the
error. Playwright names are registered only after attach succeeds, and
that is the only time the prompt tells the model to use them. If the
agent already has a Playwright MCP tool, that tool is kept and nothing
is duplicated. Set `[sandbox.browser].auto_playwright = false` to keep
the browser image and its RAM but attach MCP yourself. A failed attach
does not fail the turn, and the prompt then makes no browser or
Playwright claim. A hosted microvm session gets a size line that
states RAM only (`Sandbox size is L (2048 MiB).`). It does not claim
Chromium because the size is `L`. Chat sessions and `environment.type`
`none` get no sandbox, size, or `/workspace` text. The full fragment
table is in [config](config.md#pi).

### Packages, files, env, network, and setup commands

Session create may include `environment.packages`,
`environment.setup_commands`, `environment.env`,
`environment.files`, and `environment.network` on `openai_hosted`.
The same fields can be stored on the agent as
`session_defaults.environment`. A session that omits them, or that uses
the same environment type, inherits them. A session value replaces a
scalar or a list. `env` merges by key. `packages` merges by ecosystem
and the session list replaces that ecosystem. Files merge by path.
`inherit_agent_defaults: false` skips the agent defaults for that
session. The merged values are what the session stores. Prep runs
before the first
agent turn that needs the computer:

1. Write `files` into the session directory. Paths use the same
   `/workspace` and `/tmp/workspace` mapping as setup `cwd`. Other
   absolute paths, `..` escapes, and writes under `.apipi/` are
   rejected. `type: "inline"` uses standard base64 `data`.
   `type: "file_id"` copies bytes from a Files API upload owned by
   this tenant. The decoded total must fit
   `APIPI_MAX_WORKSPACE_BYTES`. At most 50 files per create. A missing
   or foreign `file_id` is `404`.
2. Apply `env` (string keys and values) to that session's Pi process
   and to prep. Reserved names are rejected: `PATH`, `HOME`, `USER`,
   `SHELL`, `PWD`, `LD_LIBRARY_PATH`, `LD_PRELOAD`, `OPENAI_API_KEY`,
   `OPENAI_BASE_URL`, `DATABASE_URL`, `PI_CODING_AGENT_DIR`, and any
   name starting with `APIPI_`, `CODEX_`, or `PI_`.
3. Install `packages.python`, then `packages.system`, then
   `packages.npm`. Python packages go into a virtualenv at `.venv`
   in the session workspace, not into the system Python. The install
   uses `uv` when it is on `PATH`, otherwise `python3 -m pip` inside
   that virtualenv. If neither `uv` nor `pip` is available, prep fails
   with a clear message. It does not try to install `pip` with `apk`.
   npm packages install under `.npm` in the same workspace
   (`npm install -g --prefix`). On isolation `none` and `chat`, Pi's
   `PATH` puts `.venv/bin` and `.npm/bin` first when those directories
   exist. On `microvm`, guest init does the same after prep, before Pi
   starts. `packages.system` still uses `apk` or `apt-get`. Isolation
   `microvm` has a read-only root filesystem, so `packages.system` is
   rejected with `400` at session create. A turn that still reaches
   prep fails the environment with the same message. Bake those
   packages into a guest image instead.
4. Run `setup_commands` in order. Each item is an object with
   `command` and optional `cwd`. `cwd` defaults to the session
   directory. Absolute OpenAI paths `/workspace` and `/tmp/workspace`
   map to that directory. Other absolute paths are rejected.

`network.access` is `enabled`, `disabled`, or `restricted`.
`restricted` requires `allowed_domains` (1–100 exact hostnames).
`enabled` allows outbound traffic to the public internet. Private and
special-use IPv4 ranges are always rejected. If the process-wide TAP
allowlist is on, that list still wins for public hosts. `disabled`
blocks guest TAP egress (DNS and the host broker on the TAP subnet
still work). `restricted` allows only those hostnames, plus package
registries when `packages` is set so install can run. A session cannot
add a host that `[sandbox.network]` forbids. Model and HTTP MCP calls
go through the host broker, so they still work when TAP is locked.

After a sandbox TTL wipe, the next turn recreates `/workspace` and
re-applies the stored files (inline and Files API ids), env, packages,
setup commands, and network policy.

Isolation `none` runs that script in the session directory on the host
(`uv` or `python3 -m pip` into `.venv`, `apk` or `apt-get` if present,
`npm` into `.npm`). Missing tools fail the session. Isolation `none`
cannot enforce TAP policy: `disabled` and `restricted` fail the
environment with a clear error; `enabled` is a no-op. Isolation
`microvm` packs the same script into the guest and runs it after
unpack, before Pi, in the same guest, and applies `network` on that
guest TAP. The guest root filesystem is read-only. Python and npm
installs land on the `/workspace` tmpfs, so they use guest RAM and
count against the sandbox size. They are installed again after a
sandbox stop. `packages.system` cannot write that root and is rejected.
When the optional TAP allowlist is on, install hosts (PyPI, npm,
Alpine) are added for that session if the matching package list is set.

A nonzero exit emits `agent.session.environment.failed` and
`agent.session.failed`. Pi does not start. Successful prep is visible
in the workspace before the turn. `none` and `self_hosted` environment
types reject packages, setup commands, env, and files. `self_hosted`
also rejects `network`. Environment type `none` ignores `network`.

## `none`

No computer. Pi still runs the loop. Function tools and MCP still
work. There is no session directory and no shell. This type is an
Agents API field. `/v1/chat` never asks clients to set it and never
returns `environment`. Chat sessions still store `type=none` internally
so placement can use chat workers. See [chat fleets](chat.md).

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
session starts. Hosted packs upload at `/v1/skills` and attach with
`environment.skills` `{ "type": "skill_reference", "skill_id": "…" }`.
An agent can store those references in
`session_defaults.environment.skills`. Session create unions them with
the session's own skills, agent first, and drops duplicates. A skill
deleted after the agent was saved does not block the delete. The next
session that inherits it fails with `400` and names the agent and the
skill id. Those zips unpack under `.agents/skills/` in the session
workspace. See [tools](tools.md).

Stdio MCP (for example Playwright) follows Pi, not the remote runner.
HTTP MCP is reached from the gateway and handed to Pi through the host
credential broker. Pi lists those tools from the broker URL. The guest
does not receive the bearer.
