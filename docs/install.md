# Install and run

You can install ApiPi from PyPI or from a git checkout. A laptop try
uses SQLite in the current directory and isolation `none`. Production
isolation is `APIPI_RUN_MODE=microvm`. One process can keep SQLite.
Several processes share Postgres.

Live turns need the Pi CLI (`pi --mode rpc`) on `PATH` and a model
host URL. The gateway pins Pi 0.85.1. `apipi install` can install that
Pi CLI, a Firecracker microVM, or both. On a TTY with no flags it asks
what to install (default is Pi). Without a TTY it installs Pi only, so
scripts and CI keep working. Put the Pi binary on `PATH`, or set
`APIPI_PI_COMMAND`. You can also install Pi yourself:

```
npm i -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1
```

## From PyPI

Python 3.13:

```
pip install geki-apipi
apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
apipi migrate
apipi serve
```

The import package and CLI are `apipi`. `uv add geki-apipi` works in a
project. S3-compatible artifact storage is an extra:
`pip install "geki-apipi[s3]"`.

When `DATABASE_URL` is unset, the process uses SQLite at
`.apipi/apipi.db` in the current working directory, next to
`.apipi/sessions`. Run `apipi migrate` before `apipi serve`. Isolation
defaults to `none` and logs a warning. The process binds `0.0.0.0:8000`.
`OPENAI_BASE_URL` is the model host that Pi calls.

`apipi check` verifies requirements and then exits. It leaves HTTP
unbound. `--skip-db` and `--skip-model` skip the store and the model
host. `--fast` skips the throwaway sandbox probe for `microvm`.
`apipi install` is idempotent; `--force` reinstalls Pi and MicroVM
files that are already present.

## Pi and MicroVM

Flags skip the prompt: `--pi`, `--microvm`, and `--image default|browser`.
`--dry-run` prints the commands and exits. `--image` without `--pi`
installs only the microVM.

```
apipi install --pi
apipi install --microvm
apipi install --microvm --image browser
apipi install --pi --microvm
```

`--microvm` checks `/dev/kvm`, `ip`, `iptables`, and `tc` (it names
the packages; it does not run apt). It downloads pinned Firecracker
1.17.0 and jailer into `$XDG_DATA_HOME/apipi/firecracker` (or
`~/.local/share/apipi/firecracker`). It builds the guest kernel and
rootfs with the packaged rootfs script into
`$XDG_CACHE_HOME/apipi/microvm` (or `~/.cache/apipi/microvm`). The
loop mount still needs sudo, the same way
`./scripts/microvm-rootfs` does. It prints `export` lines for the
kernel and rootfs. It does not write `.env` or `apipi.toml`, and it
does not set `APIPI_RUN_MODE`.

When those image paths are unset, `apipi serve` and
`apipi microvm shell` use the cache files if they exist. Env, `.env`,
and `[sandbox].kernel` / `rootfs` still override. Missing files fail
with `apipi install` and the path that was looked at. Firecracker and
jailer are found on `PATH`, then in that install prefix, then under
`SUDO_USER` when the process is root.

`apipi microvm shell` needs a TTY. If you are not root, it re-runs
itself with `sudo -E`, the absolute Python interpreter, and `PATH` /
`HOME` kept, so sudo `secure_path` does not need `uv`. It never runs
`sudo uv`. `apipi serve` does not re-exec. TAP and jailer still need
root or the capabilities in [run modes](run-modes.md).

## From a git checkout

```
git clone https://github.com/GEKI-AI/apipi.git
cd apipi
uv sync
uv run apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
uv run apipi migrate
uv run apipi serve
```

Prefix every `apipi` command with `uv run` while you work from the
checkout. Contributors use this path for tests and docs:
`./scripts/check`, and `./scripts/check --docs` when Markdown changed.

## Production store

One `apipi serve` can keep SQLite. File SQLite uses WAL and foreign
keys. Several processes, or HA, use Postgres. A Compose file at the
repo root starts Postgres 17 (user `apipi`, password `apipi`, database
`apipi`) on port 5432:

```
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
apipi serve
```

`postgres://` and `postgresql://` URLs are rewritten to
`postgresql+asyncpg://`. You can put the URL in `.env` or `apipi.toml`.
Give each process its own SQLite file, or share Postgres instead.

`apipi migrate` applies a single baseline revision (`0001_initial`)
that matches the current schema. Databases created before 0.1.0 have
no upgrade path through the old revision chain. Recreate the database,
then migrate. If the tables already match this schema, stamp Alembic to
`0001_initial` instead of upgrading.

## Model URL

`OPENAI_BASE_URL` is required. It is the model host Pi calls. Clients
use a different URL for this gateway. On `apipi serve`, the process
lists `{OPENAI_BASE_URL}/models`, checks that `pi --version` is 0.85.1,
and exits before it binds HTTP if those checks fail.

The model key is the request `Authorization: Bearer` value. Auth maps
that bearer to a tenant. The raw bearer stays out of Postgres. It is
passed into the live Pi process as `OPENAI_API_KEY`. Optional
`OPENAI_API_KEY_OVERWRITE` replaces that key for every session when you
want one operator key instead of the caller's bearer. A process
`OPENAI_API_KEY` is ignored.

The `agent.model` on the request must exist on that host. An unknown id
returns `400` with code `model_not_found`. Clients can list those ids
with `GET /v1/models`, which proxies to the model host unless
`APIPI_FORWARD_MODELS` is off. Pi is started with that id and a
gateway-owned `models.json`.

Live turns also need Pi on `PATH`. You can override the binary with
`APIPI_PI_COMMAND`.

## Serve

Production operators set `APIPI_RUN_MODE=microvm` so each session
boots in a Firecracker guest. That starts when `/dev/kvm`, Firecracker,
jailer, guest images, `ip`, `iptables`, and `tc` are present, and after
a throwaway guest has booted and been torn down:

```
APIPI_RUN_MODE=microvm apipi serve
```

The process default is `none` (Pi as a child of the gateway). It logs
a warning that this isolation is meant for laptops and CI:

```
APIPI_RUN_MODE=none apipi serve
```

That binds `0.0.0.0:8000` by default. `--host`, `--port`, and
`--config` change the bind and the TOML file. Startup also logs usage
store depth, retention, whether usage and payload export are on, and
whether Prometheus metrics and OpenTelemetry traces are on. Logs are
JSON lines on stderr and flush after each line. A POST logs
`request start` immediately. A turn logs `turn start`, then microVM
boot/jailer/vsock and `pi prompt` / first `pi event` while it runs.
The HTTP `request` line is written when the stream ends. Guest kernel
and Firecracker console lines are `debug` (`APIPI_LOG_LEVEL=debug`).

`microvm` reaches the model URL and HTTP MCP through a TAP device.
Guest traffic uses that TAP rather than host loopback to Postgres.
The TAP is allowlisted and rate-limited by default. Run
`apipi install --microvm` so the kernel and rootfs exist; unset, the
process uses those cache files. Production units still set explicit
paths in the environment file.

`GET /health` returns `{"status": "ok"}` without a bearer.

One `apipi serve` is one process. The Pi pool lives in that process, so
run a single uvicorn worker. Several processes need sticky
routing ([production](production.md), [multiple nodes](scale.md)).

## systemd

Run the gateway under systemd on the host with
`APIPI_RUN_MODE=microvm`. Keep secrets in an environment file that the
unit loads. MicroVM units need `/dev/kvm` and permission to create TAP
devices. Full unit examples are in [run modes](run-modes.md).

```
[Unit]
Description=ApiPi gateway
After=network.target postgresql.service

[Service]
Type=simple
WorkingDirectory=/opt/apipi
EnvironmentFile=/etc/apipi.env
ExecStart=/opt/apipi/.venv/bin/apipi serve --config /etc/apipi.toml
Restart=on-failure
DeviceAllow=/dev/kvm rw
DeviceAllow=/dev/net/tun rw
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

Environment variables in `/etc/apipi.env` override keys in the TOML
file. Bind, run mode, and the auth callback are the usual ones to set
there.

## Auth callback

Unset `APIPI_AUTH` uses the default hash: any non-empty bearer is a
tenant. For production, point `auth` at an in-process function
`package.mod:func`. A small example is `examples/auth_callback.py`. See
[auth](auth.md).
