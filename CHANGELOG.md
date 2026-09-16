# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- HTTP session routes run turns through an in-process execution
  adapter. Isolation backends stay behind that boundary.

### Added

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
- Worker drain (`heartbeat` `"drain": true`), least-loaded placement,
  and Prometheus gauges for workers, leases, and assign latency.
  Expired leases fail closed and are not reassigned.

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
