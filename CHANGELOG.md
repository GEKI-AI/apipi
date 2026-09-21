# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking

- S3 `auto` addressing is virtual-hosted. Set `APIPI_S3_ADDRESSING=path`
  for R2 or MinIO on an IP. Presigned URLs use the same style.

- MCP tools use OpenAI nested `transport` only (`http` with
  `server_url`, `stdio` with `command` / `args`). Flat `server_url` or
  `command` on the tool object is rejected. Rewrite saved agent tool
  JSON. Stdio MCP is the same API, not an ApiPi extension.
- Worker `register` requires `run_mode`. The hub picks by that
  placement class before capacity or RAM. Agents
  `environment.type=none` is placed on `chat` workers by default
  (`APIPI_ENV_NONE_PLACEMENT`). Set `microvm` to keep a computer-only
  fleet, or `reject` to fail closed with code `placement`.
- `apipi_workers` and `apipi_worker_leases` have a `run_mode` label.

### Changed

- HTTP session routes run turns through an in-process execution
  adapter. Isolation backends stay behind that boundary.
- Concepts pages for how it fits together, isolation, and workers.
  Scale docs treat worker leases as session ownership; sticky routing
  is for combined serve and `self_hosted` sockets.
- MicroVM TAP egress is public internet by default. Guest localhost
  works. The model host is always reachable. Destination allowlist is
  optional. `tc` rate limits stay.

### Added

- Host Pi Prometheus series on the worker scrape: process count, RSS,
  PSS, spawn, and kill reason (`idle` / `session` / `respawn` /
  `shutdown`).
- Host workers stamp Pi and stdio MCP with `APIPI_WORKER_PID` and reap
  leftovers from a dead worker on the next start. systemd units set
  `KillMode=control-group`.
- Presigned PUT/GET for Files, Skills, and artifact downloads when
  `APIPI_ARTIFACT_STORE=s3`. Bytes go to the bucket; complete writes
  metadata. Local store returns `presign_unsupported`.
- `/v1/chat/sessions` is a GEKI-native chat facade over the same
  session store. Clients never set or see `environment`. Chat tools
  allow function tools and HTTP MCP only (`chat_tool` on deny).
- Chat fleets operator page: `APIPI_RUN_MODE=chat` vs `microvm` on one
  API-only gateway, Agents `type=none` placement, and chat to computer
  as a new session.
- Session rows store a full `file://` or `s3://` URI for the harness
  Pi session cache.
- `APIPI_RUN_MODE=chat` is a first-class alias of isolation `none` for
  dedicated chat worker pools.
- Trusted worker WebSocket at `/internal/worker` with leases,
  heartbeats, and session ownership. This is not customer
  `self_hosted`.
- `apipi serve --api-only` skips the KVM probe. `apipi worker`
  connects outbound to the API. `check` and `install` take
  `--role api|worker|all`.
- `apipi worker` probes the sandbox before it connects. Firecracker,
  jailer, and TAP stay on the worker (or on combined `apipi serve` as
  an embedded worker). API-only hosts do not create TAP devices.
- `apipi serve --api-only` leases a worker for turns. SSE reads new
  events from the store so API nodes do not need the live Pi process.
  No worker is `429` with code `capacity`.
- Worker drain (`heartbeat` `"drain": true`), RAM-first placement,
  and Prometheus gauges for workers, leases, and assign latency.
  Expired leases fail closed and are not reassigned.
- Workers advertise `capacity` (max sessions) and `memory_mb` (RAM
  budget in MiB). The API will not lease a worker that would pass
  either cap. Among eligible workers it prefers more free RAM.
- Hosted sessions accept `environment.env` and `environment.files`
  (`type: "inline"` base64, or `type: "file_id"` from the Files API)
  under `/workspace`. Reserved env names are rejected. Values persist
  for sandbox TTL rebuild.
- Files API: `POST/GET/DELETE /v1/files` and content download. Purpose
  `user_data` or `assistants`. Bytes in the shared object store. Cap
  `APIPI_MAX_FILE_BYTES` (50 MiB).
- Skills API: `POST/GET/DELETE /v1/skills` zip upload. Attach with
  `environment.skills` `skill_reference`. Unpacks under
  `.agents/skills/`. Same 50 MiB upload cap.
- Hosted sessions accept `environment.network` (`enabled`, `disabled`,
  `restricted` with exact `allowed_domains`). Session policy cannot
  widen `[sandbox.network]`. Isolation `none` cannot enforce
  `disabled` or `restricted`.
- Session sandbox sizes `S` / `M` / `L`. `environment.sandbox_size` is
  an ApiPi extension. Stock SDKs can set `metadata["apipi.sandbox_size"]`.
  `L` boots the browser rootfs. Live guests follow the resolved size,
  not process-wide `APIPI_MICROVM_IMAGE`. Size `L` also injects Playwright
  MCP (system Chromium in the guest) unless the agent already attached
  it or `auto_playwright` is off. The platform prompt mentions the
  browser only when those tools are present.
- Rootless API Docker image and Compose service (`apipi serve
  --api-only`). Worker systemd units live in `deploy/systemd/`.

### Added

- Serve logs flush each line. A turn logs `request start`,
  `turn start`, microVM boot/vsock, and `pi prompt` while SSE is
  still open.
- MicroVM guests get virtio-rng and a host random seed so Pi is
  not stuck on `getrandom()` before TLS.
- A stuck `in_progress` session is failed or cancelled so the next
  message can run. Guest console logs are debug.
- `apipi install` asks Pi / MicroVM / both on a TTY. `--microvm`
  downloads pinned Firecracker 1.17.0 and builds guest images.
  Unset kernel and rootfs paths use those cache files when they
  exist. `apipi microvm shell` re-runs under sudo with `PATH` and
  `HOME` kept.
- `POST /v1/agents/sessions/{id}/events` accepts the OpenAI nested
  `events` envelope and the existing flat body.
- OpenAI compatibility page with comparison tables for routes, fields,
  lifecycle, and errors.

## [0.2.0] - 2026-09-15

### Added

- `apipi microvm shell` boots the same Firecracker guest as agent
  sessions and attaches a serial shell. TAP, NAT, `ip_forward`, and
  jailer failures name the step and say when root or `CAP_NET_ADMIN`
  is missing.
- `apipi install` installs the pinned Pi CLI. `apipi check` verifies
  requirements without binding HTTP.
- Unset `DATABASE_URL` uses SQLite at `.apipi/apipi.db`. SQLite is
  enough for one process. Postgres when the store is shared.
  `apipi migrate` applies schema before `apipi serve`.

### Changed

- `output_text.delta` is live SSE only. Reconnect and export use
  `output_text.done` and items.
- File SQLite uses WAL. Production docs list when Postgres is
  required.
- Logs are JSON lines on stderr by default. `APIPI_LOG_FORMAT=text`
  is the laptop opt-in.
- Product docs use complete sentences. README and Get started cover
  install from PyPI and from a git checkout.

## [0.1.0] - 2026-09-14

First public release. Install from PyPI as `geki-apipi`. The import
package and CLI stay `apipi`.

### Added

- OpenAI Agents API compatible gateway (`apipi serve`) with Postgres as
  the source of truth.
- Pi harness over RPC. Run modes `none` (local/dev) and `microvm`
  (Firecracker). Production uses `microvm`.
- Agents, sessions, events, turns, items, artifacts, and export.
- Tenant-scoped auth via a callback. The gateway maps the bearer to a
  tenant.
- Optional S3-compatible artifact storage (`geki-apipi[s3]`).

### Changed

- Alembic history is a single baseline revision (`0001_initial`) that
  matches the current schema. Pre-0.1.0 databases have no upgrade path
  through the old revision chain. Recreate the database and run
  `apipi migrate`, or stamp `0001_initial` if the schema already
  matches.

[Unreleased]: https://github.com/GEKI-AI/apipi/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.2.0
[0.1.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.1.0
