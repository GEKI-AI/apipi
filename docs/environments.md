# Environments

An environment is where file and shell tools run. That choice is
independent of [isolation](isolation.md) (run mode), which is where Pi
itself runs. Pi and the computer always share one isolation boundary, and there is no split.

## Types

| `environment.type` | When |
| --- | --- |
| `openai_hosted` | Default. Session directory next to Pi. |
| `hosted` | Alias for `openai_hosted`. Stored and returned as `openai_hosted`. |
| `none` | No filesystem, no shell. |
| `self_hosted` | Currently not supported (see below). |

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
`M`, or `L`. That field is an ApiPi extension. OpenAI clients set
`environment.container_size` (`small` / `medium` / `large`) instead,
which is stored as `sandbox_size`. Other `metadata` keys stay
opaque tags. The `apipi.` prefix is reserved. The gateway reads
`apipi.sandbox_image`.
Extenders may set `apipi.actor_type`, `apipi.schedule_id`, and
`apipi.source`. The gateway stores those keys and does not schedule
from them. The full list is in
[reserved metadata](extending.md#reserved-metadata).

Resolution, highest wins:

1. `environment.sandbox_size` or `environment.container_size` on the session
2. Agent `session_defaults.environment.sandbox_size`
3. Gateway `APIPI_SANDBOX_DEFAULT_SIZE` / `[sandbox].default_size`
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
Each image has a minimum size. `browser` and `work` need `M` or
larger. A smaller size is `400`. Isolation `none` stores the field
and does not apply it. `work` is the business-document image. It
includes libraries for Excel, Word, PowerPoint, PDF, CSV, and charts.
It does not include LibreOffice or pandoc. Select it with
`sandbox_image` `work` and size `M` or `L`. Install it with
`apipi install --microvm --image work` or `apipi images pull`.

The built-in `browser` skill is packed when the resolved image is
`browser`, not because the size is `L`. The shipped default still
maps `L` to `browser`, so existing `L` sessions keep that image.

| Size | Guest RAM | Rootfs | When |
| --- | --- | --- | --- |
| `S` | `[sandbox.resources].mem_mib` (512) | `default` | Pi and light tools |
| `M` | `APIPI_SANDBOX_M_MEM_MIB` (1024) | `default` | Heavier non-browser work |
| `L` | `APIPI_SANDBOX_L_MEM_MIB` (2048) | `browser` | agent-browser and chrome-headless-shell, at least 2 vCPUs by default. Operators can change this with `APIPI_SANDBOX_IMAGE_MIN_VCPUS`. Install the browser rootfs. It is x86_64 only. |

Isolation `none` accepts the field and does not apply RAM or rootfs.
Isolation `microvm` applies both, including when `environment.type` is
`none` (Pi still runs in a guest). Each live lease consumes that
size's RAM against worker `memory_mb` and still counts as one session.

On `microvm`, image `browser` packs the built-in `browser` skill and
starts no browser process until the first `agent-browser` call. The
daemon then stays up for the session. Cookies persist for that
session. A new session or a new sandbox is clean, because the profile
and socket live on `/tmp`. Size `L` still selects `browser` when the
image is omitted. Install that rootfs with
`apipi install --microvm --image browser`. The image is x86_64 only.
An aarch64 worker does not offer it, so the placement error is
`image_unavailable` and names the architecture. Browser guests get at
least 2 vCPUs by default, including size `M`. Operators can change this
with `APIPI_SANDBOX_IMAGE_MIN_VCPUS`. `/dev/shm` is 512 MiB on the
browser image and 64 MiB on the others. Chrome still uses `/tmp` for
shared memory because the image sets `--disable-dev-shm-usage`.

The image sets these defaults in `/etc/apipi/browser.env`:

| Variable | Value | Why |
| --- | --- | --- |
| `AGENT_BROWSER_EXECUTABLE_PATH` | `/opt/chrome-headless-shell/chrome-headless-shell` | Pinned Chrome for Testing binary, not `agent-browser install`. |
| `AGENT_BROWSER_ARGS` | `--no-sandbox,--disable-dev-shm-usage` | Set explicitly. The guest runs as root, so upstream would add both anyway. |
| `AGENT_BROWSER_IDLE_TIMEOUT_MS` | `0` | Upstream shuts the daemon down after one hour idle, which would drop cookies mid-session. |
| `AGENT_BROWSER_NO_WEBMCP` | `1` | WebMCP is experimental and adds page-tool announcements ApiPi does not expose. |
| `AGENT_BROWSER_SOCKET_DIR` | `/tmp/agent-browser` | Keep the socket off the workspace. |
| `AGENT_BROWSER_SCREENSHOT_DIR` | `/workspace/.browser/screenshots` | Inspection screenshots are working files. |
| `AGENT_BROWSER_DOWNLOAD_PATH` | `/workspace/.browser/downloads` | Downloads are working files. |

The stream WebSocket is left on. Upstream always starts it on an
OS-assigned port bound to `127.0.0.1`, and there is no env switch to
turn it off at daemon start. Nothing in ApiPi consumes it. The
dashboard stays off because `AGENT_BROWSER_DASHBOARD` is unset. Cloud
providers stay off because `AGENT_BROWSER_PROVIDER` is unset. There is
no update check or telemetry to disable. Do not run `agent-browser
install` or `agent-browser upgrade`. A project file
`./agent-browser.json` is still merged over these defaults if the
session creates one.

`/workspace/outputs` is not a working directory. Only artefacts the
user explicitly asked for go there. Working files go in
`/workspace/.browser` or `/tmp`. When the user asks for a screenshot,
copy that one file to `outputs/`.

A hosted microvm session gets a capability line that names the image,
size, RAM, vCPUs, and network. It does not claim a browser because the
size is `L`. The browser sentence is present only when the image is
`browser`. `environment.type` `none` gets no sandbox,
size, or `/workspace` text. The full fragment table is in
[config](config.md#pi).

A local x86_64 `default` image used about 1.1 GiB of its 2048 MiB
filesystem. A local `browser` image used about 1.7 GiB of its 4096
MiB filesystem. Cold start, RAM, and crash recovery were not measured
in a live VM for this change. The first `agent-browser open` starts the
daemon and Chrome. Plan on the M=1024 MiB guest being tight once
Chrome is up.
Upstream launches Chrome with `--headless=new` even for
chrome-headless-shell. That flag is harmless for a binary that is
already headless, but it has not been confirmed in this guest. PDF
uses CDP `Page.printToPDF`. Screenshot, PDF, download, a Chrome crash
restart, and unasked downloads still need a VM measurement before you
treat those paths as proven.

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
   that virtualenv. Guest images include both. If neither `uv` nor
   `pip` is available, prep fails
   with a clear message. It does not try to install `pip` with `apt`.
   npm packages install under `.npm` in the same workspace
   (`npm install -g --prefix`). On isolation `none`, Pi's
   `PATH` puts `.venv/bin` and `.npm/bin` first when those directories
   exist. On `microvm`, guest init does the same after prep, before Pi
   starts. `openai_hosted` sessions are always placed on a `microvm`
   worker, and the guest has a read-only root filesystem, so the API
   rejects `packages.system` with `400` at session create whenever
   `type=openai_hosted`, whatever run mode the API itself is configured
   with. A turn that still reaches prep fails the environment with the
   same message. Bake those packages into a guest image instead.
4. Run `setup_commands` in order. Each item is an object with
   `command` and optional `cwd`. `cwd` defaults to the session
   directory. Absolute OpenAI paths `/workspace` and `/tmp/workspace`
   map to that directory. Other absolute paths are rejected.

Hosted workspaces include an `inputs/` directory. Put files the user
provided there, including `environment.files` paths under `inputs/`.
Existing paths still work. `inputs/` is not published. Only `outputs/`
is.

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
(`uv` or `python3 -m pip` into `.venv`, `apt-get` if present, `npm`
into `.npm`). Missing tools fail the session. Isolation `none`
cannot enforce TAP policy: `disabled` and `restricted` fail the
environment with a clear error; `enabled` is a no-op. Isolation
`microvm` packs the same script into the guest and runs it after
unpack, before Pi, in the same guest, and applies `network` on that
guest TAP. The guest root filesystem is read-only. Python and npm
installs land on the `/workspace` tmpfs, so they use guest RAM and
count against the sandbox size. They are installed again after a
sandbox stop. `packages.system` cannot write that root and is rejected.
When the optional TAP allowlist is on, install hosts (PyPI, npm,
Debian) are added for that session if the matching package list is set.

A nonzero exit emits `agent.session.environment.failed` and
`agent.session.failed`. Pi does not start. Successful prep is visible
in the workspace before the turn. `none` rejects packages, setup commands, env, and files. Environment type `none` ignores `network`.

## `none`

No computer. Built-in tools are always off and cannot be turned on:
`apipi.builtin_tools=on` on a `type=none` session is `400` with code
`builtin_tools`, and so is `apipi.codemode` `on` or `only`. Pi still
runs the loop. Function tools and HTTP MCP with `server_url` still
work; anything else is `400` with code `tool_not_allowed`. There is no
session directory and no shell. Pi for `type=none` always runs directly
on the worker host. Saved agents that still carry
`metadata.apipi.session_kind=chat` are ignored for placement now.
See [sandbox workers](workers.md#placement).

## `self_hosted`

`self_hosted` is currently not supported. Requests with
`environment.type` `self_hosted` (session create, agent
`session_defaults`, template import) return `400` with type
`not_implemented` and the message `environment type self_hosted is not
supported`. Use `openai_hosted` instead. The type may come back later
on worker protocol v2 (see #442).

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

Tools run where Pi runs. The guest browser is a
skill, not MCP.
HTTP MCP is reached from the gateway and handed to Pi through the host
credential broker. Pi lists those tools from the broker URL. The guest
does not receive the bearer.
