# Install and run

You can install ApiPi from PyPI or from a git checkout. A laptop try
uses SQLite in the current directory and isolation `none`. Production
isolation is `APIPI_RUN_MODE=microvm` on **workers** (or on combined
`apipi serve` on one box). One process can keep SQLite. Several
processes share Postgres. How isolation and workers fit is in
[Concepts](concepts.md).

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
defaults to `none` and logs a warning. Unset
`APIPI_VAULT_MASTER_KEY` uses a local default for MCP vault tokens and
logs a warning; set a 32-byte key in production. The process binds
`0.0.0.0:8000`. `OPENAI_BASE_URL` is the model host that Pi calls.

`apipi check` verifies requirements and then exits. It leaves HTTP
unbound. `--skip-db` and `--skip-model` skip the store and the model
host. `--fast` skips the throwaway sandbox probe for `microvm`.
`--role api|worker|all` matches how the host will run (default `all`
is combined API plus sandbox). `apipi install` is idempotent; `--force`
reinstalls Pi and MicroVM files that are already present.
`apipi install --role api` installs nothing extra. `--role worker`
installs MicroVM.

## Pi and MicroVM

Flags skip the prompt: `--pi`, `--microvm`, and
`--image default|browser|work`.
`--dry-run` prints the commands and exits. `--image` without `--pi`
installs only the microVM.

```
apipi install --pi
apipi install --microvm
apipi install --microvm --image browser
apipi install --microvm --image work
apipi install --pi --microvm
```

`--image browser` is the durable way to install the browser rootfs.
That image contains agent-browser, a pinned chrome-headless-shell,
and Noto fonts. It is x86_64 only. The model uses bash and the
built-in `browser` skill. It does not start Playwright MCP. After
upgrading ApiPi, run `apipi install --microvm --image browser` again
so workers pick up that rootfs. `--image work` installs the
business-document image.
It needs sandbox size `M` or larger. See [run modes](run-modes.md).

`--microvm` pulls a prebuilt image when `APIPI_IMAGE_SOURCE` is set.
`--build` keeps the local recipe build for an air-gapped host.
`--dry-run` prints which of those it would run.

The guest kernel is Linux 6.1.186, pinned in `images/kernel.env`.
An existing cache `vmlinux` whose `vmlinux.version` does not match is
replaced on the next `apipi install --microvm`. A pulled image set
uses the kernel published with that set. Rebuild or pull after this
change so guests are not still on the old 4.14 kernel.

`--microvm` checks `/dev/kvm`, `ip`, `iptables`, and `tc` (it names
the packages; it does not run apt). It downloads pinned Firecracker
1.17.0 and jailer into `$XDG_DATA_HOME/apipi/firecracker` (or
`~/.local/share/apipi/firecracker`). With `APIPI_IMAGE_SOURCE` set it
pulls verified images into the images dir. Without that setting, and
with `--build`, it builds from `images/<id>/` into
`$XDG_CACHE_HOME/apipi/microvm` (or `~/.cache/apipi/microvm`). The
loop mount for a local build still needs sudo, the same way
`./images/build.sh` does. `./scripts/microvm-rootfs` is a wrapper for
that script. A pull prints the images dir. A build prints `export`
lines for the kernel and rootfs. It does not write `.env` or
`apipi.toml`, and it does not set `APIPI_RUN_MODE`.

The Firecracker tarball also contains `.debug` binaries. Install
copies the release `firecracker` and `jailer` only. It skips that
download when both `firecracker --version` and `jailer --version`
exit 0 and report the pinned version. A missing or crashing jailer
is replaced on the next `apipi install --microvm` without `--force`.
`--force` still replaces binaries that already pass those checks.

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

## Mirror, verify, and pull guest images

The official store for a release is the GitHub release assets at
`https://github.com/GEKI-AI/apipi/releases/download/v<version>/`.
Mirror that prefix, verify it, then pull on each worker. None of those
commands need root, a loop device, or a chroot.

```
apipi images mirror --from 0.12.0 --to s3://bucket/images
apipi images verify --source s3://bucket/images --version 0.12.0
apipi images pull
```

`--from 0.12.0` means the official release. `--from` can also be an
HTTPS prefix, an `s3://` prefix, or `file://`. `--to` is an `s3://` or
`file://` base. The command writes `<to>/v<version>/` and refuses to
overwrite a complete prefix whose checksums differ. `https://` is
read-only. A worker with `image_source` set to that base and no
`image_store_version` pulls `v<this ApiPi version>`. Set
`APIPI_IMAGE_STORE_VERSION` to roll back to an older compatible store.
An explicit missing version fails. A defaulted missing version can use
`versions.json` on a mirror. The official GitHub base has no
`versions.json`, so that version must exist exactly.

`apipi images verify --no-signature` checks `SHA256SUMS` and the digest
chain without Sigstore. Signature checks need `uv sync --extra images`.

## Custom images

Build once, push to a store, and pull on every worker. `apipi images
build <id>` runs the recipe in `images/<id>/` and writes a zstd rootfs
plus `manifest.json` under the build directory (default
`$XDG_CACHE_HOME/apipi/image-build`). It needs the same root, loop
mount, and packages as `./images/build.sh`. `--arch` only checks that
you asked for this host. Cross-build is not supported.

`apipi images push` uploads the newest local build of each image id
and arch. Newest means the manifest `created_at`, not the file name.
Older manifests left in the build directory are ignored. `apipi images
publish` is the same command. `--to` is `s3://bucket/prefix` or
`file:///path`. When `--to` is omitted, push uses `APIPI_IMAGE_SOURCE`
or `[sandbox].image_source`. If neither is set, push exits with an
error. `https://` is read-only and is rejected.

If that exact image version is already in the store and the sha256
matches, push prints a skip line and exits 0. `--force` uploads again.
`--dry-run` prints the object names and does not write. A missing S3
bucket fails with a clear error. A missing `index.json` in an existing
bucket starts a new index. The pushed version becomes `latest` for
that id and arch.

S3 endpoint, region, and addressing come from `APIPI_IMAGE_S3_ENDPOINT`,
`APIPI_IMAGE_S3_REGION`, and `APIPI_IMAGE_S3_ADDRESSING` when those are
set. Otherwise push and pull use `APIPI_S3_ENDPOINT`, `APIPI_S3_REGION`,
and `APIPI_S3_ADDRESSING`. Credentials are
`APIPI_IMAGE_S3_ACCESS_KEY_ID` and `APIPI_IMAGE_S3_SECRET_ACCESS_KEY`,
or the profile `APIPI_IMAGE_S3_PROFILE`. If none of those is set, the
AWS credential chain is used, including the instance role. Keys are
never read from TOML. Install the client with `uv sync --extra s3`.

`apipi images push --store-version 0.12.0` writes a schema 2 prefix.
Without that flag, push writes a deprecated schema 1 store and warns.
`apipi images pull` installs the pinned store version for this host's
arch into the images directory. It checks the compressed sha256, decompresses as a
stream, then checks the ext4 sha256. Peak RAM does not grow with the
image size. `apipi images list --remote` compares the local images
with that source. See [production](production.md).

The Images workflow builds the official x86_64 `default` and `browser`
images and attaches them to a GitHub release. It does not build `work`
or aarch64. Build those locally if you need them. After that workflow
runs, the release asset URL is an `https://` image source.

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

`apipi migrate` applies Alembic revisions (`0001_initial`, then
`0002_workers`). Databases created before 0.1.0 have no upgrade path
through the old revision chain. Recreate the database, then migrate.

## Docker API

The Compose file can run Postgres and a **rootless** API container.
The image runs `apipi serve --api-only`. It does not get `/dev/kvm`
or TAP. Workers stay on Linux hosts:

```
export OPENAI_BASE_URL=http://your-model-host/v1
export APIPI_WORKER_TOKEN=secret
docker compose up --build
```

That publishes Postgres on `5432` and the API on `8000` at
`0.0.0.0`. Set `OPENAI_BASE_URL` or serve exits. Put
`APIPI_WORKER_TOKEN` in the environment so workers can connect.
`self_hosted` runners still attach to `/v1/environments/{id}` on the
API; they are not the worker.

On a KVM host:

```
apipi install --role worker
APIPI_API_URL=http://api.example:8000 APIPI_WORKER_TOKEN=secret \
  APIPI_RUN_MODE=microvm apipi worker
```

Unit files are in `deploy/systemd/`.

## Three ways to run

Everything starts through the `apipi` CLI. `uvicorn` is not a
supported operator path.

### Combined (one host)

Laptop or a single server. API and sandbox share one process.

```
apipi install
export OPENAI_BASE_URL=http://your-model-host/v1
apipi check
apipi migrate
apipi serve
```

Isolation defaults to `none`. For Firecracker on that same box:

```
apipi install --microvm
export APIPI_RUN_MODE=microvm
apipi check --role all
apipi serve
```

`apipi serve` probes the run mode. If `microvm` cannot start, the
process exits.

### Split (API + worker)

Rootless API, KVM on another host. Example Compose plus a worker:

```
# API host (or docker compose up --build)
export OPENAI_BASE_URL=http://your-model-host/v1
export APIPI_WORKER_TOKEN=secret
apipi check --role api
apipi migrate
apipi serve --api-only
```

```
# KVM host
apipi install --role worker
export OPENAI_BASE_URL=http://your-model-host/v1
export APIPI_WORKER_TOKEN=secret
export APIPI_API_URL=http://api.example:8000
export APIPI_RUN_MODE=microvm
apipi check --role worker
apipi worker
```

The API never opens `/dev/kvm`. The worker probes Firecracker before
it connects. Put `APIPI_WORKER_TOKEN` in the process environment, not
in the browser. Example `apipi.toml` keys: `worker_token` is allowed
but secrets belong in `.env`.

```
# .env on the API and on each worker
APIPI_WORKER_TOKEN=secret
OPENAI_BASE_URL=http://your-model-host/v1
DATABASE_URL=postgresql+asyncpg://apipi:apipi@db:5432/apipi
```

Set `APIPI_VAULT_MASTER_KEY` on the API to a 32-byte key (base64 or
hex) so MCP vault tokens are not encrypted with the local default.

```
# extra on the worker
APIPI_API_URL=http://api.example:8000
APIPI_RUN_MODE=microvm
```

### Several workers

One API tier, many KVM hosts, shared Postgres. Start more
`apipi worker` processes with the same token and API URL. Each worker
advertises `capacity` (from `APIPI_MAX_SESSIONS`) and `memory_mb` (from
`APIPI_WORKER_MEMORY_MB`, default `max_sessions × mem_mib`). Placement
picks the worker with the most free RAM among those that still have a
session slot and enough remaining RAM. A turn with no lease returns
`429` with code `capacity`. Drain a worker with a heartbeat
`"drain": true` before you stop it.

API replicas do not need sticky routing for Pi. See
[multiple nodes](scale.md).

## Model URL

`OPENAI_BASE_URL` is required. It is the model host Pi calls. Clients
use a different URL for this gateway. On `apipi serve` and
`apipi worker`, the process checks that `pi --version` is 0.85.1 and
exits before it binds if that check fails. With the default
`APIPI_MODEL_LIST=probe` it also lists `{OPENAI_BASE_URL}/models` at
start and exits if that list fails. `turn` and `off` do not call
`/models` at start. See [configuration](config.md#model-host).

The model key is the request `Authorization: Bearer` value. Auth maps
that bearer to a tenant. The raw bearer stays out of Postgres. It is
passed into the live Pi process as `OPENAI_API_KEY`. Optional
`OPENAI_API_KEY_OVERWRITE` replaces that key for every session when you
want one operator key instead of the caller's bearer. A process
`OPENAI_API_KEY` is ignored.

Creating or editing an agent checks `agent.model` against the model
list. An unknown id returns `400` with code `model_not_found`. Turns
do not repeat that check. If the host rejects the model later, the
turn fails. In this release the public code is still
`model_host_error`. The specific code is `detail_code`. See
[failure codes](errors.md). Clients can list ids with
`GET /v1/models`, which proxies to the model host unless
`APIPI_FORWARD_MODELS` is off or `APIPI_MODEL_LIST` is `off`. Pi is
started with that id and a gateway-owned `models.json`.

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

`apipi serve` is the combined path: API plus a local sandbox in one
process. `apipi serve --api-only` is the control plane only. It does
not probe KVM or start Firecracker, so it can run in rootless Docker.
`apipi worker` connects outbound to that API (`APIPI_API_URL`,
`--url`, or `http://127.0.0.1:8000`) with `APIPI_WORKER_TOKEN`. See
[sandbox workers](workers.md).

That binds `0.0.0.0:8000` by default. `--host`, `--port`, and
`--config` change the bind and the TOML file. Startup also logs usage
store depth, retention, whether usage and payload export are on, and
whether Prometheus metrics and OpenTelemetry traces are on. Logs are
JSON lines on stderr and flush after each line. A POST logs
`request start` immediately with the URL path. A turn logs `turn start`, then microVM
boot/jailer/vsock and `pi prompt` / first `pi event` while it runs.
The HTTP `request` line is written when the stream ends. Guest kernel
and Firecracker console lines are `debug` (`APIPI_LOG_LEVEL=debug`).

`microvm` reaches the model URL and HTTP MCP through a TAP device.
Guest traffic uses that TAP rather than host loopback to Postgres.
The TAP may use the public internet and is rate-limited. Private and
special-use IPv4 ranges are rejected. An optional destination
allowlist can lock the guest to named public hosts.
Run
`apipi install --microvm` so the kernel and rootfs exist; unset, the
process uses those cache files. Production units still set explicit
paths in the environment file.

`GET /health` returns `{"status": "ok"}` without a bearer.

One `apipi serve` is one process. The Pi pool lives in that process, so
run a single uvicorn worker. Combined serve with several processes
needs sticky routing. API-only plus workers does not, for live Pi.
See [production](production.md) and [multiple nodes](scale.md).

## systemd

Run the API under systemd as `apipi serve --api-only`. Run guests as
`apipi worker` with `APIPI_RUN_MODE=microvm`. Keep secrets in an
environment file that the unit loads. Worker units need `/dev/kvm` and
permission to create TAP devices. Files are in `deploy/systemd/` and
[run modes](run-modes.md).

Example units: `deploy/systemd/apipi-api.service` (`serve --api-only`,
no KVM) and `deploy/systemd/apipi-worker.service` (DeviceAllow for
`/dev/kvm` and TAP). Both worker units need `KillMode=control-group`
so a stop or crash restart does not leave host Pi processes. Combined
serve on one box can still use `apipi serve` with
`APIPI_RUN_MODE=microvm` if that host is the hypervisor.

Environment variables in `/etc/apipi.env` override keys in the TOML
file. Bind, run mode, worker token, and the auth callback are the
usual ones to set there.

## Auth callback

Unset `APIPI_AUTH` uses the default hash: any non-empty bearer is a
tenant. For production, point `auth` at an in-process function
`package.mod:func`. A small example is `examples/auth_callback.py`. See
[auth](auth.md).
