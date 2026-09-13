# Install and run

This page is how you install ApiPi and start the API process. Settings,
files, and every environment variable are in [config](config.md). Point
a client at the running server with the [quickstart](quickstart.md).

## Requirements

Python 3.13 or newer and [uv](https://docs.astral.sh/uv/) only. Do not
use pip or a bare `python -m venv`.

Postgres is required. A Compose file at the repo root starts a local
Postgres 17 server with user `apipi`, password `apipi`, and database
`apipi`, published on host port 5432.

Live turns need the Pi CLI (`pi --mode rpc`) on `PATH` and a model URL.
The gateway pins Pi 0.85.1.

Run mode `jail` needs `bwrap`, `pasta`, and cgroup v2. Run mode
`microvm` needs `/dev/kvm`, `firecracker`, `jailer`, kernel and rootfs
images, `ip`, and `iptables`. If the selected mode cannot start, the
process exits. There is no silent fallback. Packages, systemd, Docker,
and when to use each mode are in [run modes](run-modes.md).

## Install

From a checkout:

```
uv sync
```

That installs the `apipi` CLI into the project environment. After
`uv sync` you can run `apipi` from that environment, or prefix commands
with `uv run`.

## Database

Start local Postgres and apply store migrations:

```
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
```

`DATABASE_URL` is required. `postgres://` and `postgresql://` URLs are
rewritten to `postgresql+asyncpg://`. SQLite is for tests only and is
rejected by `apipi serve` and `apipi migrate`.

You can put `DATABASE_URL` in `.env` or `apipi.toml` instead of
exporting it. See [config](config.md).

## Model URL

Pi talks to your model with the usual OpenAI environment variables.
`OPENAI_BASE_URL` is the model host, not this gateway.
`OPENAI_API_KEY` is the key that host expects. Those values are passed
into the Pi process. Pi does not receive `DATABASE_URL` or gateway
secrets.

Live turns also need Pi on `PATH`. You can override the binary with
`APIPI_PI_COMMAND`.

## Serve

The configured default run mode is `jail`. That starts when `bwrap`,
`pasta`, and cgroup v2 are present:

```
apipi serve
```

If jail tools are missing, the process exits. Operators without those
tools must set `host`:

```
APIPI_RUN_MODE=host apipi serve
```

That binds `0.0.0.0:8000` by default. `--host`, `--port`, and
`--config` change the bind and the TOML file. `host` runs Pi as a child
of the gateway. The process logs a warning:
`APIPI_RUN_MODE=host is not suited for production`. `jail` does not log
that warning. Startup also logs that the turn log is on, and whether
Prometheus metrics and OpenTelemetry export are on.

`microvm` needs `/dev/kvm`, Firecracker, jailer, `APIPI_MICROVM_KERNEL`,
`APIPI_MICROVM_ROOTFS`, `ip`, and `iptables`. The guest reaches the
model URL and HTTP MCP through a TAP device. There is no host loopback
to Postgres.

`GET /health` returns `{"status": "ok"}` and does not require a bearer.

One `apipi serve` is one process. The Pi pool lives in that process.
Do not run uvicorn workers in front of it.

## systemd

Production is systemd on the host. Keep secrets out of the unit file.
Jail units need `Delegate=yes` so cgroup memory works. Microvm units
need `/dev/kvm` and permission to create TAP devices. Full unit
examples are in [run modes](run-modes.md).

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
Delegate=yes
DelegateControllers=memory pids

[Install]
WantedBy=multi-user.target
```

Environment variables in `/etc/apipi.env` override keys in the TOML
file. Bind, run mode, and the auth callback are the usual ones to set
there. Do not set `NoNewPrivileges=yes` on a jail unit; bubblewrap
needs user namespaces.

## Auth callback

Unset `APIPI_AUTH` uses the default hash: any non-empty bearer is a
tenant. For production, point `auth` at an in-process function
`package.mod:func`. A small example is `examples/auth_callback.py`. See [auth](auth.md).
