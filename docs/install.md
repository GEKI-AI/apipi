# Install and run

There are two paths. **Try it** is a laptop: SQLite in the current
directory, isolation `none`, no Docker. **Production** is Postgres,
`APIPI_RUN_MODE=microvm`, and the rest of this page.

Live turns need the Pi CLI (`pi --mode rpc`) on `PATH` and a model
host URL. The gateway pins Pi 0.85.1. `apipi install` installs that
exact version with npm into a user-local prefix. Put that binary on
`PATH`, or set `APIPI_PI_COMMAND`. The manual one-liner is still valid:

```
npm i -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1
```

## Try it

Python 3.13. From PyPI:

```
pip install geki-apipi
apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
apipi serve
```

The import package and CLI stay `apipi`. `uv add geki-apipi` works in a
project. S3-compatible artifact storage is an extra:
`pip install "geki-apipi[s3]"`.

Unset `DATABASE_URL` uses SQLite at `.apipi/apipi.db` in the current
working directory, next to `.apipi/sessions`. `apipi serve` applies
migrations before it binds HTTP. Isolation defaults to `none` and logs
a warning. That binds `0.0.0.0:8000`. `OPENAI_BASE_URL` is the **model**
host, not this API.

`apipi check` does not bind HTTP. It exits non-zero when a required
check fails. `--skip-db` and `--skip-model` skip the store and the
model host. `--fast` skips the throwaway sandbox probe for `microvm`.
`apipi install` is idempotent; `--force` reinstalls Pi.

From a checkout (contributors): `uv sync`, then prefix commands with
`uv run`.

## Production store

Production uses Postgres. A Compose file at the repo root starts
Postgres 17 (user `apipi`, password `apipi`, database `apipi`) on port
5432:

```
docker compose up -d postgres
export DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
apipi migrate
apipi serve
```

`postgres://` and `postgresql://` URLs are rewritten to
`postgresql+asyncpg://`. You can put the URL in `.env` or `apipi.toml`.
Do not share a SQLite file across processes or nodes.

`apipi migrate` applies a single baseline revision (`0001_initial`)
that matches the current schema. Pre-0.1.0 databases have no upgrade
path through the old revision chain. Recreate the database, then
migrate. If the tables already match this schema, stamp Alembic to
`0001_initial` instead of upgrading.

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
