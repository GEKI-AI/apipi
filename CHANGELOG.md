# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Agent create and update validate `metadata["apipi.sandbox_size"]` and
  `metadata["apipi.sandbox_image"]`. A bad size, an unknown image, or a
  size below the image minimum is `400`. Worker availability is still a
  session placement error. Docs now say the image selects the rootfs,
  with size `L` mapping to `browser` when the image is omitted.

## [0.5.1] - 2026-09-25

### Security

- Presigned GET URLs force `Content-Disposition: attachment` with the
  original file name, including an RFC 5987 `filename*` when the name is
  not ASCII. HTML, SVG, XML, and JavaScript are signed as
  `application/octet-stream` so a browser does not render them from the
  bucket domain. Gateway `/content` routes use the same attachment
  header and send `X-Content-Type-Options: nosniff`.

### Fixed

- S3 and botocore errors during harvest, cache restore, or hosted file
  and skill setup fail the turn or environment with code
  `artifact_store` instead of an unhandled `internal` error. Gateway
  reads and uploads return `503` with that code. A missing object is
  still a normal miss.

## [0.5.0] - 2026-09-24

### Added

- Prebuilt MicroVM guest images. Recipes live in `images/`.
  `apipi images build` and `apipi images publish` write a zstd rootfs,
  a manifest, and an index to `file://` or `s3://`. `apipi images pull`
  and `apipi install --microvm` install them. `environment.sandbox_image`
  and `metadata["apipi.sandbox_image"]` choose the image. Workers
  advertise images. A missing image is `503` `image_unavailable`.
- Optional `idle_ttl` on an agent and on session create. Resolve order
  is session, then agent, then the environment-type default. `0` turns
  the timer off for that session.
- Pi compaction, thinking level, and the harness system prompt are
  written into the session agent directory before Pi starts.
  `compaction.enabled` replaces the unused `--no-auto-compact` flag.
  Session `metadata["apipi.thinking"]` and
  `metadata["apipi.system_prompt"]` override the process defaults.
- Public events `agent.session.turn.compaction.started` and
  `agent.session.turn.compaction.completed`. Missing compaction events
  do not fail the turn.
- Every MicroVM guest image built from `images/` includes `curl` and
  `git`.

### Fixed

- `apipi worker` starts the idle Pi and workspace reap loops. Split
  deploys kill idle sessions. `apipi serve --api-only` still does not.

## [0.4.0] - 2026-09-24

### Fixed

- `apipi install --microvm` installs the release Jailer, not the
  `.debug` binary from the Firecracker tarball. A missing or crashing
  Jailer is replaced on the next install without `--force`.
- The microVM guest no longer receives the worker process environment.
  Guest `.apipi/env` keeps the broker URL, a dummy `OPENAI_API_KEY`,
  that session's MCP settings, and `environment.env`. It does not hold
  `APIPI_WORKER_TOKEN`, database settings, or `OPENAI_API_KEY_OVERWRITE`.
- MicroVM TAP egress rejects private and special-use IPv4, including
  RFC1918, link-local, and `100.64.0.0/10`. The public internet stays
  open. The TAP subnet stays open so the guest can reach the host broker.

## [0.3.2] - 2026-09-22

### Added

- Pi thinking level `APIPI_PI_THINKING`. Stored thinking events carry a
  100-character preview, `duration_ms`, and `reasoning_tokens`. The full
  thinking text is not a public event.
- Optional thinking summaries through a sidekick model. The platform
  flag `APIPI_THINKING_SUMMARY` and the auth callback must both allow it.
- Optional automatic session titles in `metadata["apipi.title"]` through
  the same sidekick. `APIPI_AUTO_TITLE` and a separate auth callback
  flag must both be on.
- Reserved session metadata for extenders that drive runs:
  `apipi.actor_type`, `apipi.schedule_id`, and `apipi.source`. The
  gateway stores them and does not schedule from them.

### Fixed

- A streamed create that cannot start the first turn emits
  `agent.session.error` with a code and `agent.session.failed`, then
  the stream ends.
- Session delete stops the live guest before it removes the row. A
  worker started with sudo deletes artifact files it owns.

## [0.3.1] - 2026-09-22

### Changed

- MCP vault tokens are encrypted at rest with AES-256-GCM
  (`APIPI_VAULT_MASTER_KEY`). Unset uses a local default and logs a
  warning. `apipi migrate` rewrites leftover plaintext rows.

## [0.3.0] - 2026-09-21

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

### Fixed

- Chat and `environment.type=none` write broker `models.json` even
  without a workspace, so text-only turns can reach the model. Pi
  stderr is logged. Non-stream create returns `502` with `session_id`
  when the first turn fails.
- Artifact harvest I/O errors (`PermissionError` and other `OSError`)
  fail the turn with code `artifact_store` instead of `500 internal`.
- Request-start logs use the URL path. Shutdown harvest ignores
  `CancelledError`.
- Hosted follow-up after a sandbox TTL wipe fails cleanly. The Pi
  session cache reloads for the next turn.

### Changed

- Run `apipi migrate` for schema revisions `0002` through `0010`
  (workers, leases, files, skills, Pi session URI, uploads).
- `stream: true` on session create returns SSE as soon as the session
  row exists. The first turn runs in the background.
- HTTP session routes run turns through an in-process execution
  adapter. Isolation backends stay behind that boundary.
- Concepts pages for how it fits together, isolation, and workers.
  Scale docs treat worker leases as session ownership; sticky routing
  is for combined serve and `self_hosted` sockets.
- MicroVM TAP egress is public internet by default. Guest localhost
  works. The model host is always reachable. Destination allowlist is
  optional. `tc` rate limits stay.
- Artifact harvest publishes only workspace `outputs/`.
- Model and MCP secrets stay on a host credential broker, not in the
  guest.

### Added

- Optional host Pi memory ceiling `APIPI_PI_MEM_MIB` / `[pi].mem_mib`:
  Node heap hint plus RSS kill (`reason=memory`) so one session cannot
  silently fill a chat worker.
- `apipi worker` treats SIGTERM/SIGINT as drain: heartbeat
  `"drain": true`, wait until live Pi are gone, exit 0 (timeout exits
  1). Example systemd drop-in `deploy/systemd/apipi-worker-drain.conf`.
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
- Production observability guide. Worker Prometheus metrics and
  optional guest samples. Turn metrics and traces on the worker.
  Wait-focused spans honor `traceparent`. Structured error events
  with ids and codes.
- Optional auth `user_id` on usage events and export.
- `Gateway` handle for in-process wiring. Extending docs and a
  webpage-check example. `tenant_from_key` for callers without HTTP
  auth.
- Overridable platform prompt before agent instructions.

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

[Unreleased]: https://github.com/GEKI-AI/apipi/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/GEKI-AI/apipi/releases/tag/v0.3.1
[0.3.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.3.0
[0.2.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.2.0
[0.1.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.1.0
