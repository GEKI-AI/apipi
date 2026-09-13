# Install and run

You need Python 3.13, [uv](https://docs.astral.sh/uv/), and Postgres.
Live turns also need the Pi CLI (`pi --mode rpc`) on `PATH` and a model
host URL. The gateway pins Pi 0.85.1. Install that exact version:

```
npm i -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1
```

A Compose file at the repo root starts Postgres 17 (user `apipi`,
password `apipi`, database `apipi`) on port 5432.

Firecracker (`APIPI_RUN_MODE=microvm`) needs `/dev/kvm`, `firecracker`,
`jailer`, kernel and rootfs images, `ip`, `iptables`, and `tc`. Isolation
`none` runs Pi as a child of the gateway. If the selected mode cannot
start, `apipi serve` exits before it binds HTTP.

## Install

From a checkout:

```
uv sync
```

That installs the `apipi` CLI into the project `.venv`. It does not put
`apipi` on `PATH`. Prefix commands with `uv run`, or activate the
environment with `source .venv/bin/activate`.

## Database

Start local Postgres and apply store migrations:

```
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
uv run apipi migrate
```

`DATABASE_URL` is required. `postgres://` and `postgresql://` URLs are
rewritten to `postgresql+asyncpg://`. You can put it in `.env` or
`apipi.toml` instead of exporting it.

## Model URL

`OPENAI_BASE_URL` is required. It is the model host Pi calls, not this
gateway. On `apipi serve`, the process lists `{OPENAI_BASE_URL}/models`,
checks that `pi --version` is 0.85.1, and exits before it binds HTTP if
those checks fail.

The model key is the request `Authorization: Bearer` value. Auth only
maps that bearer to a tenant. The raw bearer is not stored in Postgres.
It is passed into the live Pi process as `OPENAI_API_KEY`. Optional
`OPENAI_API_KEY_OVERWRITE` replaces that key for every session when you
want one operator key instead of the caller's bearer. A process
`OPENAI_API_KEY` is ignored.

The `agent.model` on the request must exist on that host. An unknown id
returns `400` with code `model_not_found`. Pi is started with that id
and a gateway-owned `models.json`. It does not fall back to Pi's
built-in OpenAI catalog.

Live turns also need Pi on `PATH`. You can override the binary with
`APIPI_PI_COMMAND`.

## Serve

Production operators set `APIPI_RUN_MODE=microvm` so each session
boots in a Firecracker guest. That starts when `/dev/kvm`, Firecracker,
jailer, guest images, `ip`, `iptables`, and `tc` are present, and after
a throwaway guest has booted and been torn down:

```
APIPI_RUN_MODE=microvm uv run apipi serve
```

The process default is `none` (Pi as a child of the gateway). It logs
a warning that this isolation is meant for laptops and CI:

```
APIPI_RUN_MODE=none uv run apipi serve
```

That binds `0.0.0.0:8000` by default. `--host`, `--port`, and
`--config` change the bind and the TOML file. Startup also logs usage
store depth, retention, whether usage and payload export are on, and
whether Prometheus metrics and OpenTelemetry traces are on.

`microvm` reaches the model URL and HTTP MCP through a TAP device.
There is no host loopback to Postgres. That TAP is allowlisted and
rate-limited by default. Set `APIPI_MICROVM_KERNEL` and
`APIPI_MICROVM_ROOTFS`.

`GET /health` returns `{"status": "ok"}` and does not require a bearer.

One `apipi serve` is one process. The Pi pool lives in that process, so
extra uvicorn workers are a poor fit. Several processes need sticky
routing ([production](production.md), [multiple nodes](scale.md)).

## systemd

Run the gateway under systemd on the host with
`APIPI_RUN_MODE=microvm`. Keep secrets out of the unit file. MicroVM
units need `/dev/kvm` and permission to create TAP devices. Full unit
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
`package.mod:func`. A small example is `examples/auth_callback.py`. See [auth](auth.md).
