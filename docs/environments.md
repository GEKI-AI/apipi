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

Search and browser are not environments. Search is the built-in
`web_search` tool or an MCP server, and the browser is a skill in the
`browser` image. See [tools](tools.md).

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
conversation. Files from `environment.files` are the starting state of
the workspace, not a copy that is kept in sync: an agent edit to such a
file stays until the workspace is wiped, and after the wipe the
original comes back. Published files are not copied back into `/workspace`.
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

1. Write `files` into the session directory. A file is written only
   when its path does not exist in the session directory yet. A path
   that exists is left as it is, even when the agent changed or
   replaced the file, and the worker does not fetch its bytes from the
   store. Paths use the same
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
is. The agent may edit or delete files under `inputs/`. Edits and
deletions last until the workspace is rebuilt. In isolation `none` a
deleted file is written again from the store before the next turn. In
`microvm` it stays missing until the guest stops, and the next boot
starts from the original files. See the restore rules below.

### Attachments

Hosted workspaces also have an `attachments/` directory for files that
users attach to messages. A message may carry `input_file` parts (see
[events](api.md#events)). In a session with a computer each file goes
to `attachments/<filename>` in the workspace, not to the model, and Pi
can open it with its tools in the same turn. Agent inputs and session
files are two kinds of workspace files and stay separate:

| Kind | Directory | Comes from | Belongs to |
| --- | --- | --- | --- |
| Agent inputs | `inputs/` (or any path) | `environment.files` at session create, or the agent's `session_defaults` | The starting state of every session of the agent |
| Session files | `attachments/` | `input_file` parts of user messages | One session. Other sessions, also of the same agent, never see them. |

The gateway binds each attached file to the session with its path
before the turn starts. Session files are not added to
`environment.files`, and the session's `environment` does not change.
The name is the `filename` of the part, or the stored file name, cut to
its last path segment. When the session already has a file at that
path, or an agent input uses it, the file gets a free name like
`report (2).xlsx`. The name is chosen on the API in one transaction
with the binding, so two messages that arrive at the same time get two
names. A file that is already bound to the session with a path keeps
that path when it is attached again. The user item and the prompt carry
the final path.

The turn context lists every session file of the session, not only the
new ones. Before Pi starts the turn, the worker writes the files of the
current message at their paths, and replaces whatever is there: a file
the agent created or changed at that path, or an older attachment whose
file was deleted from the Files API, so its path was free again. The
agent always sees the file the user attached with this message. This
also means that attaching the same `file_id` again resets the file to
its original content. The other session files of earlier messages are
restored with the same "write only when missing" rule as
`environment.files`: a file the agent changed stays changed until the
sandbox is wiped, and after a TTL wipe or a restart the next turn writes
every session file again under the same path. A file deleted from the
Files API is no longer bound to the session, so it is not restored, and
the turn does not fail.

In `microvm` a guest that is already running does not see new files in
the session directory, so the worker also copies the files of the
current message into the running guest over a vsock port before the
turn, and the guest replaces a file at the same path. If that copy
fails, the turn does not start: the session gets
`agent.session.environment.failed` and `agent.session.error` with code
`attachment_push_failed`, which is retryable, and the worker stops the
guest (stop reason `push_failed`). Send the message again. The next turn
boots a new guest from the session directory, which already has the
files.

Pi learns about the new files of a turn from one line per file in the
user message, at the place of the part, for example
`Attached: attachments/report.xlsx (xlsx, 240 KB)`. The hosted prompt
tells the agent that `attachments/` holds files from the conversation
that are restored after a restart like `inputs/`.

Each file must be within `APIPI_MAX_FILE_BYTES`. A message may carry up
to `APIPI_MAX_FILES_PER_MESSAGE` files. The agent inputs and all
session files of the session together must fit
`APIPI_MAX_WORKSPACE_BYTES`. The gateway checks these limits from the
stored file sizes before the turn starts and answers `413` with code
`payload_too_large` when one is exceeded.

`network.access` is `enabled`, `disabled`, or `restricted`.
`restricted` requires `allowed_domains` (1–100 exact hostnames).
`enabled` allows outbound traffic to the public internet. Private and
special-use IPv4 ranges are always rejected. If the process-wide TAP
allowlist is on, that list still wins for public hosts. `disabled`
blocks all guest TAP egress, DNS included. The guest reaches only the
host broker on the TAP host IP. `restricted` allows only those hostnames, plus package
registries when `packages` is set so install can run. A session cannot
add a host that `[sandbox.network]` forbids. Model and HTTP MCP calls
go through the host broker, so they still work when TAP is locked.

In `microvm`, the policy is enforced by hostname, not by IP address.
Guest TCP to ports 80, 443, and 8443 goes to an egress gateway in the
worker process. The guest needs no proxy settings. On 443 and 8443 the
gateway reads the server name (SNI) from the TLS handshake. On 80 it
reads the `Host` header. UDP to port 443 is rejected, so QUIC clients
fall back to TCP. Private and special-use addresses are rejected in
every mode. The guest can reach the worker host only on the broker,
gateway, and DNS filter ports of its own session.

With `restricted` (or the process-wide TAP allowlist), a hostname must
match an allowed name exactly (case does not matter). A connection
without a server name, with a server name that is not a valid
hostname, or with an IP address as the server name or `Host`, is
rejected. The gateway resolves the allowed name itself, checks every
address against the private ranges, and connects to the address it
resolved, not to the address the guest asked for. DNS rebinding or a
changed `/etc/hosts` in the guest therefore cannot reach another
address. On port 80 the gateway reads every HTTP request on the
connection. Each request must name the same allowed host in `Host`, a
request with a full URL must name that host too, and `CONNECT` is
rejected. TCP to other ports and other UDP traffic is rejected. Guest
DNS goes to a filtering resolver on the TAP host IP. It forwards
queries for allowed names, answers `NXDOMAIN` for every other name, and
sends upstream only a new query built from the name and type, so DNS
cannot carry data out. It answers HTTPS and SVCB queries with no
records.

On TLS connections that are passed through, `restricted` checks only
the server name. The gateway does not see the encrypted request, so it
cannot stop domain fronting (a `Host` header for another site behind
the same CDN) or read the inner name of Encrypted Client Hello. Allow
only hosts that you trust with that.

With `enabled`, public hosts are allowed, IP addresses included. The
gateway connects to the address the guest asked for after checking that
it is not private, so `curl --resolve` and `/etc/hosts` in the guest
work as before. Other ports use the direct NAT path, and DNS goes
directly to the public resolvers. `disabled` does not start a gateway.

Allowed connections are passed through unchanged, so the guest still
sees the real certificate of the server. A connection that sends no
data for 5 minutes is closed. Each session may have up to 128 open
connections through the gateway.

After a sandbox TTL wipe, the next turn recreates `/workspace` and
re-applies the stored files (inline and Files API ids), env, packages,
setup commands, and network policy. The wipe deletes the session
directory, so every file is missing and the worker fetches and writes
all of them again in their original version.

The check for a missing file runs against the session directory on the
worker host before each turn. In isolation `none` that directory is the
workspace Pi uses, so an agent edit stays there until the TTL wipe. In
`microvm` the session directory is only the seed of the guest: it is
packed into the workspace drive when the guest boots, and the guest
does not write back to it. The first boot after a wipe finds the
directory empty and writes every file before the drive is packed. A
turn on a running guest finds the files that were written for its
boot, so it fetches no file bytes. Agent edits live on the guest tmpfs
and last until the guest stops. The next boot starts from the original
files that are still in the session directory.

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

### Vault credentials and the network

A vault credential of type `environment_variable` lets code in the
guest call HTTPS APIs with a key that the guest never holds. The guest
environment sets the credential's `secret_name` to a placeholder, and
the egress gateway on the worker replaces it in the request headers of
HTTPS requests on ports 443 and 8443 to the credential's `allowed_hosts`
and masks the secret again in the responses. For those hosts only, the gateway terminates TLS with a certificate from the
worker's certificate authority, which guest init adds to the guest's
trusted bundle `/run/apipi/ca-bundle.pem` (see
[configuration](config.md#networking)). Credential hosts are HTTPS
only: a plain HTTP connection on port 80 to one of them is rejected with
`403`. See
[Vaults and credentials](vaults.md#how-environment-credentials-work).

These credentials depend on `network.access`:

- `enabled`: the credential hosts are reachable like any public host.
- `restricted`: the `allowed_hosts` of every attached environment
  credential are added to `allowed_domains` when the sandbox starts.
  You do not list them twice. The stored `environment.network` keeps
  only the hosts you wrote.
- `disabled`: session create with environment credentials is `400`
  with code `credential_not_allowed`, because no request could leave
  the guest.

When the operator TAP allowlist is on, every credential host must also
be allowed by the operator, or session create is `400` with code
`credential_host_not_allowed`. A credential host on a private network
works only when the operator lists it in
`APIPI_MICROVM_EGRESS_PRIVATE_HOSTS`. A `secret_name` that is also a
key in `environment.env` is `400` with code `secret_name_collision`.
`environment.type` `none` has no guest, so environment credentials
there are `400` with code `credential_not_allowed`.

## `none`

No computer. Built-in tools are always off and cannot be turned on:
`apipi.builtin_tools=on` on a `type=none` session is `400` with code
`builtin_tools`, and so is `apipi.codemode` `on` or `only`. Pi still
runs the loop. Function tools and HTTP MCP with `server_url` still
work, and so does the built-in `web_search` tool; anything else is
`400` with code `tool_not_allowed`. There is no session directory and
no shell. Pi for `type=none` always runs directly
on the worker host. Saved agents that still carry
`metadata.apipi.session_kind=chat` are ignored for placement now.
See [sandbox workers](workers.md#placement).

## Web search

The built-in `web_search` tool works on `type=none` and on
`openai_hosted` sessions in every run mode. It needs no computer and
no guest network. On `none` Pi runs on the worker host and calls the
session broker on loopback. In a microVM the guest calls the same
per-session broker on the TAP host IP, so the guest reaches only the
broker, as it does for the model and for HTTP MCP. The worker forwards
the call to the API over its socket. The provider name and key never
enter the guest or the worker. The per-session `environment.network`
policy needs no search host, because the guest never connects to the
provider. See [tools](tools.md#web-search) and
[workers](workers.md#messages).

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
