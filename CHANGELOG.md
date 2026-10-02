# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Artifacts via API-issued presigned PUT with no object-store credentials on workers (#448). The split worker holds no blobs or objects and performs no artifact or file database writes: `LocalExecution` runs credential-less and `OutboxSink` uploads through `artifact.presign` (outbox) -> `artifact.presign.reply` (same socket) -> PUT (S3, plain HTTPS) or shared-root write (filesystem, to the reply `path`) -> `artifact.completed` (outbox), for `artifact`, `pi_session`, and `input_image` kinds, including input images in `run_turn` and the killed-process harvest. The killed harvest uses the tenant identity from the live sinks and the workspace directory remembered from the turn context, with no database access. The total workspace limit is enforced on the worker from settings in both split harvest paths, with the same `workspace_too_large` code as combined mode. Combined serve keeps today's direct path through `DirectSink`. The worker sends durable `artifact.presign` (session id, kind, filename, content type, size, checksum; never bytes); the API checks quotas (`max_workspace_bytes`, `max_artifact_bytes`, or `max_file_bytes` for input images) before reserving the slot and answers with `upload_id`, `url`/`headers`/`expires_at` (S3) or the exact store-root relative `path` (filesystem), plus `object_id` and `file_id` for input images. The API verifies the object (S3 `HEAD` size and checksum, or shared-root size and checksum), rejects a foreign `upload_id`, a path that does not match the reserved key, and checksum or size mismatches, then writes rows with the presigned `artifact_id` (artifacts), the file row with `file_id` (input images), or the session Pi pointer the turn context uses for cold restore (Pi sessions). When the presigned digest matches the latest stored bytes for that artifact path, the API answers `unchanged` instead of reserving a slot (before quota, as in combined mode) and the worker skips the upload with no new row. Pi presigns reuse the existing blob id so saves overwrite one object. Quota failures return today's codes and fail the turn the way direct writes do. New `APIPI_LOCAL_STORE_DIR` is the dedicated local store root (defaults to `APIPI_SESSIONS_DIR`, so combined mode is unchanged); turn-context references now use it, and split mode with `local` and no explicit shared root fails fast with a message that points at `s3` or a shared root. At register the API writes a nonce marker into the root and sends it in `hello.reply`; a worker that cannot read it back is rejected with `filesystem store requires a shared path`. New `artifact_uploads` ledger (migration `0027`). Combined `apipi serve` is documented as test and dev only; production is `apipi serve --api-only` plus `apipi worker`. See `docs/workers.md`, `docs/scale.md`, `docs/production.md`, and `docs/config.md`.
- Live delta relay over the worker socket (#445): in split mode the
  worker coalesces model text fragments over about 40ms per session
  and sends them as ephemeral v2 `delta.text` envelopes instead of
  publishing them itself. The API replica holding the socket checks
  that the session is leased to that worker, applies a 32 KiB size
  cap and a per-session rate budget (100 deltas per second), drops
  deltas for turns that already committed `output_text.done` (the
  final item stays the source of truth), and publishes the rest as
  `live` messages on the event bus without any store write, so SSE
  token streaming works on any replica with no sticky routing.
  `delta.reasoning` envelopes are accepted but never fanned out.
  Outcomes are counted in `apipi_worker_protocol_total`. See
  `docs/workers.md` (Live deltas) and `docs/scale.md`.
- Event bus with Postgres `LISTEN`/`NOTIFY` fan-out for session
  events. After the API commits stored events it publishes a wake
  (`session_id`, `seq`); SSE streams wait for wakes instead of
  polling every 250ms, and `RemoteExecution._wait` waits on the same
  mechanism instead of polling every 50ms. A fallback poll
  (`APIPI_EVENT_BUS_FALLBACK_POLL`, default `3s`) covers lost
  notifications and listener reconnects, so an idle stream queries
  the store no more often than that interval. `APIPI_EVENT_BUS` is
  `auto` (Postgres on a Postgres store, in-memory on SQLite),
  `memory`, or `postgres`; explicit `postgres` on SQLite fails at
  startup. Each Postgres replica holds one dedicated `LISTEN`
  connection outside the SQLAlchemy pool, and each payload carries
  its sender so a replica never re-delivers its own broadcast.
  Until durable worker ingest (#446) the worker still writes events
  itself, so `apipi worker` builds the same bus from its own
  `DATABASE_URL` and must point at the same database as the API (turn
  context still comes from that database until #447). Live `output_text.delta`
  batches are coalesced over about 40ms and stay under the
  8000-byte `NOTIFY` limit (larger batches are split); deltas are
  never stored. New metrics: `apipi_event_bus_listener_reconnects_total`,
  `apipi_pg_notification_queue_usage`, and
  `apipi_event_bus_wake_sse_seconds`. SSE no longer needs sticky
  routing on a Postgres store (see `docs/scale.md`).
- Worker commands (`turn.start`, `turn.continue`, `sandbox.boot`) carry a
  `context` object built by the API: session environment and identity,
  the resolved agent definition, the effective idle TTL, the HTTP MCP
  servers with vault headers applied, the model key, and references
  (never bytes) to workspace files, skills, and the Pi session blob.
  The worker prepares the turn from the context instead of reading the
  session, agent, file, and skill rows, so HTTP MCP tools work in split
  mode on first and follow-up turns, including follow-ups on another API
  replica. With `APIPI_ARTIFACT_STORE=s3` references are presigned GET
  URLs; with the filesystem store they are paths relative to the shared
  store root. Contexts are validated (bytes rejected, 256 KiB command
  cap) and never logged. The workspace reaper uses the context TTL with
  no database read when the worker knows the session.

- Agent option to disable built-in tools: `metadata["apipi.builtin_tools"]`
  (`on`, `off`, default `on`) on the agent or session, with the session
  value winning over the agent. `off` starts Pi with
  `--no-builtin-tools` when MCP or function tools are present and
  `--no-tools` otherwise, plus `--no-skills`, so skills are not loaded
  and no `capability.json` is written. The guest still boots and still
  collects `outputs/` artifacts; the platform prompt uses the `none`
  fragment. An invalid value is `400` (`invalid_request`). The key is
  portable in agent bundles, and `apipi.codemode` is portable now too.
- Durable worker events ingested by the API (#446). The turn runtime
  reports turns, items, events, usage, and errors through a
  `ResultSink` interface instead of writing to the store directly.
  Combined `apipi serve` uses a direct-DB sink, so its behaviour is
  unchanged. A split-mode worker buffers durable v2 envelopes
  (`item.added`, `item.done`, `turn.status`, `usage`, `event`,
  `session.status`, `error`) in a bounded per-session outbox until
  the cumulative `ack{last_seq}`, with an optional disk spool
  (`APIPI_WORKER_OUTBOX_DIR`, capped by
  `APIPI_WORKER_OUTBOX_MAX_MESSAGES` and
  `APIPI_WORKER_OUTBOX_MAX_BYTES`). The API ingests each
  connection's envelopes in batches (about 50ms via
  `APIPI_WORKER_INGEST_BATCH_WINDOW`, or
  `APIPI_WORKER_INGEST_BATCH_SIZE` messages), one transaction per
  batch: a `worker_ingest` ledger (`UNIQUE(session_id, worker_seq)`)
  makes replays idempotent, the public `events.seq` stays
  API-assigned, and `sessions.worker_seq` (migration `0026`)
  records the ack cursor that `hello.reply` reports, so a reconnect
  to any replica replays exactly what is missing. Ingest validates
  the lease, the turn binding, and size limits; violations drop the
  message, log `worker.event.rejected`, and count in
  `apipi_worker_protocol_total{event="envelope_rejected"}`.
  `artifact.completed` is now applied by #448; `sandbox.status` stays rejected until
  #449 applies it. After commit the API sends the ack, then
  publishes the `EventBus` wake. The worker keeps the turn running
  across a dropped socket or an API restart within the lease TTL;
  a full outbox fails the turn with `worker_outbox_full`. Tool and
  MCP tallies for the turn log are collected in memory while the
  turn runs. See `docs/workers.md`, `docs/worker-concepts.md`, and
  `docs/config.md`.
- Lifecycle, sandbox status, reaper, and wipe as events, inventory
  reconcile, and API-owned lease takeover (#449). The worker makes
  no database queries. Sandbox transitions travel as durable
  `sandbox.status` envelopes, which the API applies with the same
  `record_transition` logic, so the `environment.*` events look the
  same to clients; the periodic seen update travels as an idempotent
  `sandbox.seen` summary that the API validates against the
  connection's leases. The wipe after `session.stop` and the idle
  workspace wipe travel as `session.stopped` and `workspace.reaped`
  receipts; the worker wipes only its local directory, and only
  `session.stopped` deletes the session blobs on ingest
  (`workspace.reaped` is a receipt only). The reaper uses the command
  context TTL, and sessions it does not know wait for the inventory
  reply, which carries the effective idle TTL per session, instead
  of reading the database. Lifecycle export moved to the API: the
  worker reports live start and stop as durable `lifecycle.start`
  and `lifecycle.stop` envelopes (buffered across disconnects and
  replayed exactly once), the periodic `inventory` live set is what
  the API derives heartbeats from, and retry and backoff stay on
  the API. `apipi worker` ignores `APIPI_LIFECYCLE_*` settings with
  a startup warning. On hello and about every 60s the worker sends
  `inventory{sessions}`; the API fails leases it no longer reports
  (`agent.session.error` with code `worker_orphaned`, then clears
  the lease) and revokes sessions the worker reports without a
  lease. A reconnect to any replica renews the reattached leases,
  so running turns stay alive and the lease moves to the new
  replica. Commands are idempotent by `command_id`: a retransmitted
  `turn.start` is acked but never dispatched twice. See
  `docs/workers.md` (inventory, lease takeover), `docs/usage.md`
  (lifecycle export), and `docs/config.md` (lifecycle settings are
  API-only). No new migration.
- Review round 2 on top: an unleased on-disk workspace whose
  session row is still alive gets a TTL answer (a released lease
  only means the Pi stopped; the workspace stays until its idle
  TTL) and is revoked only once the row is gone. The TTL answer
  also carries the idle baseline (`idle_since_epoch`, the row's
  last touch) so re-answering it every inventory does not restart
  the reaper clock. Only `session.stopped` deletes the session
  blobs; `workspace.reaped` is a receipt only, so a session leased
  again before its receipt lands keeps its artifacts.
- Review round 1 on top: command dedupe is worker-lifetime (a replay
  after reconnect is still recognised; ids are forgotten on
  teardown, stop, or revoke). Lease takeover renews exactly the
  reported leases with a conditional `UPDATE` matching `worker_id`
  and `lease_id` (register already points
  `workers.api_instance_id` at the new replica); heartbeats still
  extend all of the worker's leases. The orphan rule spares leases
  with a command still unacked (marked before the grant commits, so
  a grant in flight cannot orphan). Lifecycle identity always comes
  from the session row (the worker only adds sandbox, run mode, and
  timing fields, which also keeps the strict payload validation
  happy), and exports are deferred past the ingest commit so a
  replayed batch cannot export twice. Sandbox phase updates live in
  one shared function used by the local transition path and the
  ingest. The inventory also reports on-disk workspaces the worker
  holds no lease for: rows that are still alive get a TTL answer so
  the reaper wipes them when the TTL runs out, and only a missing
  row gets a revoke that wipes the directory at once.
- Workers run without database or object-store credentials, with TLS on the worker socket (#450). `run_worker` no longer creates a `Store`: the turn runtime resolves every write through the result sink, so combined mode keeps opening a real session while a split worker (outbox sink, turn context in every command) never touches the database, and the worker event bus is always in memory. A command without a turn context fails fast on a database-less worker instead of reading the store. `apipi worker` refuses to start when `DATABASE_URL` is set, and `prepare_worker` no longer warns about the vault master key, since workers never decrypt vaults. TLS is required for non-loopback API URLs (`https://` or `wss://`; loopback `http://` stays allowed for local development), and mutual TLS is optional via `APIPI_WORKER_CLIENT_CERT` / `APIPI_WORKER_CLIENT_KEY` with an optional `APIPI_WORKER_SERVER_CA` bundle for the API server certificate (terminate TLS and verify the client certificate on the proxy). A restarted worker adopts the API persisted cursor (`hello.reply` last seq) before appending, so replayed sequence numbers are never mistaken for duplicates. The split-mode end-to-end suite runs full turns with tools and MCP, artifacts, cold restore, and streaming with the worker constructors blocked. This resolves #441 item 3. See `docs/workers.md` (What runs where, Transport security), `docs/scale.md`, `docs/worker-concepts.md`, `docs/production.md` (token rotation, transport security), and `docs/config.md`.

### Breaking

- The `/v1/apipi/chat` facade is removed (all 11 routes return `404`). Text-only sessions are Agents API sessions with `environment.type=none`, which allows function tools and HTTP MCP with `server_url` only (anything else is `400`). The `chat` run mode, `src/apipi/worker/pi/isolation/chat.py`, the `apipi.session_kind` metadata key, and `APIPI_ENV_NONE_PLACEMENT` / `[placement].env_none` are removed. Saved agents that still carry `apipi.session_kind=chat` are ignored for placement; bundle import drops the key with a warning and exports no longer carry it. Workers now advertise an accepts set with `APIPI_WORKER_ACCEPTS` / `[worker].accepts` (comma list from `none`, `microvm`; default `none,microvm` on a microVM backend, else `none`). If `microvm` is listed but the microVM backend cannot run, the worker fails fast before it registers. Pi for `type=none` always runs directly on the worker host. `placement_for` returns `none` for `type=none` and `microvm` otherwise; `pick` uses set membership with least-loaded logic and no fallback (`429` `capacity`). Migrate:

  | Old | New |
  | --- | --- |
  | `POST /v1/apipi/chat/sessions` | `POST /v1/agents/sessions` with `"environment": {"type": "none"}` |
  | `GET /v1/apipi/chat/sessions` | `GET /v1/agents/sessions` |
  | `GET` / `POST` / `DELETE /v1/apipi/chat/sessions/{id}` | the same verbs on `/v1/agents/sessions/{id}` |
  | `POST` / `GET /v1/apipi/chat/sessions/{id}/events` | `/v1/agents/sessions/{id}/events` |
  | `GET /v1/apipi/chat/sessions/{id}/turns[/{turn_id}]` | `/v1/agents/sessions/{id}/turns[/{turn_id}]` |
  | `GET /v1/apipi/chat/sessions/{id}/items` | `/v1/agents/sessions/{id}/items` |
  | `GET /v1/apipi/chat/sessions/{id}/export` | `GET /v1/apipi/sessions/{id}/export` |
  | `APIPI_RUN_MODE=chat` workers | workers with `APIPI_WORKER_ACCEPTS=none` |
  | `APIPI_ENV_NONE_PLACEMENT` | removed; `type=none` goes to any worker whose accepts set contains `none` |
  | `apipi.session_kind=chat` | `environment.type=none` |
- `environment.type=self_hosted` is not supported for now. Session create, agent `session_defaults`, and template import with that type return `400` with type `not_implemented` and the message `environment type self_hosted is not supported`. The runner WebSocket (`/v1/environments/{environment_id}`), `EnvironmentHub`, runner client (`examples/self_hosted_runner.py`), `APIPI_SANDBOX_TTL_SELF_HOSTED` / `[sandbox.ttl].self_hosted`, and the `self_hosted` prompt fragments are removed. `GET /v1/agents/environments/{id}` stays for hosted computers. The type may come back later on worker protocol v2. Use `openai_hosted` as the workaround.
- Old route aliases outside `/v1/apipi` are removed and return `404`. Routers now serve directly under `/v1/apipi/...`. Migrate:
  - `GET /v1/usage` -> `GET /v1/apipi/usage`
  - `POST /v1/templates`, `POST /v1/templates/import`, `GET /v1/templates`, `GET /v1/templates/{id}`, `GET /v1/templates/{id}/download`, `DELETE /v1/templates/{id}`, `POST /v1/templates/{id}/agents` -> the same paths under `/v1/apipi/templates/...`
  - `POST /v1/uploads`, `POST /v1/uploads/{id}/complete` -> `POST /v1/apipi/uploads`, `POST /v1/apipi/uploads/{id}/complete`
  - `/v1/chat/sessions...` -> `/v1/apipi/chat/sessions...` (the `/v1/apipi/chat` routes themselves are unchanged)
  - `GET /v1/agents/{id}/export` -> `GET /v1/apipi/agents/{id}/export`
  - `GET /v1/agents/sessions/{id}/export` -> `GET /v1/apipi/sessions/{id}/export`
  - `POST /v1/agents/sessions/{id}/artifacts/{artifact_id}/download` -> `POST /v1/apipi/sessions/{id}/artifacts/{artifact_id}/download`
  - `POST /v1/files/{id}/download` -> `POST /v1/apipi/files/{id}/download`
  - `POST /v1/skills/{id}/download` -> `POST /v1/apipi/skills/{id}/download`
- Agent versions and snapshots are removed. The versions routes
  (`/v1/apipi/agents/{id}/versions`, including create, list, get,
  restore, and delete) are gone and return `404`, and
  `APIPI_AGENT_VERSIONS_KEEP` is removed (still setting it logs a
  warning and is ignored). Migration `0022` drops the
  `agent_versions` table, so existing snapshots are deleted. Export
  any you need before upgrading. To keep snapshots, use the agent
  bundle export (`GET /v1/apipi/agents/{id}/export`) and store the
  zip yourself.
- Agent `revision` is removed from the agent object, and the
  `x-apipi-agent-revision` model-host header is no longer sent.
  `x-apipi-session-id`, `x-apipi-turn-id`, and `x-apipi-agent-id`
  are unchanged.
- Agent export no longer takes `?version=`, and new bundles have no
  `source_version` (import still accepts old bundles and ignores
  it).
- Pi's built-in MCP client is the only client. The gateway no longer
  probes MCP servers at session create: the shape, headers, vault,
  and SSRF guard are still checked there, but a dead server or one
  that rejects unauthenticated calls no longer fails session create.
  Server failures surface at turn time as `pi.extension_error` and do
  not fail the turn. The per-turn `mcp_list_tools` items are gone.
  Agent sessions rebuild the server list from the live agent at every
  turn, so agent edits apply to the next turn without recreating the
  session.
- `APIPI_ERROR_CODES` now defaults to `specific`. `agent.session.error` and the non-stream `502` body carry the specific failure code in `code` (copied in `detail_code`), with `legacy_code` still `model_host_error` on upstream failures. Set `APIPI_ERROR_CODES=legacy` to keep `model_host_error` in `code` for one release. Clients that match `model_host_error` on `agent.session.error` or the `502` body should match the specific code (or `detail_code`) instead.
- The legacy guest-image cache (`~/.cache/apipi/microvm/{vmlinux,rootfs.ext4,rootfs-browser.ext4}`) is removed. The image store is the only source: run `apipi images pull <id>`. `APIPI_MICROVM_IMAGE` / `[sandbox].image` and `APIPI_MICROVM_ROOTFS_BROWSER` / `[sandbox].rootfs_browser` are removed; `apipi microvm shell` and `apipi install` take the image from `[sandbox].default_image` or `--image`. `APIPI_MICROVM_KERNEL` / `[sandbox].kernel` and `APIPI_MICROVM_ROOTFS` / `[sandbox].rootfs` stay as dev-only overrides. `apipi install --microvm --build` is removed; use `apipi images build`, `apipi images push --store-version <v>`, and `apipi images pull`. `apipi images publish` is removed (use `push --store-version`). Push without `--store-version` now fails instead of writing a deprecated schema-1 store. Migration: `apipi images pull <id>`.
- `metadata["apipi.sandbox_size"]` is removed: session create/update, agent create/update, and template/bundle import with that key return `400` (set `environment.container_size` (`small` / `medium` / `large`) or `environment.sandbox_size` (`S` / `M` / `L`)). `container_size` is the input and is stored as `sandbox_size`. Bundle import migrates the key to `session_defaults.environment.sandbox_size` with a warning when the environment has no size. `metadata["apipi.sandbox_image"]` stays as the stock-SDK path for `environment.sandbox_image`. `metadata["apipi.thinking"]` is removed as client input (`400`, set `reasoning.effort` with `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`); the gateway still stores the resolved level under that key but strips it from public session/agent `metadata` (the `reasoning` field shows the level, so echoing returned metadata back is safe). Bundle import migrates the key to `reasoning.effort` with a warning, and bundles carry the level in `reasoning`. The `idle_ttl` field wins over `metadata["apipi.idle_ttl"]`, which stays for stock SDKs. Eager boot is session metadata, then agent metadata, then `APIPI_SANDBOX_EAGER_BOOT` (the `session_defaults` lookups are removed). `hosted` is normalised to `openai_hosted` on input; downstream checks only accept `openai_hosted`. The internal setting `workspace_ttl` is renamed to `sandbox_ttl_openai_hosted` (env `APIPI_SANDBOX_TTL_OPENAI_HOSTED` and `[sandbox.ttl].openai_hosted` are unchanged).
- Prompt fragments move into `[pi.prompts]`. The `APIPI_PLATFORM_*` fragment env vars (including `*_FILE`) are removed; set `[pi.prompts]` keys (`identity.none`, `identity.computer`, `main.none`, `main.hosted`, `additional.none`, `additional.hosted`, `capability`, `browser`, `mcp_tool`) instead. The `size`, `network`, `network.enabled`, and `network.restricted` fragments and their prompt files are removed, with `network_hint`, `sandbox_size_hint`, and the `settings is None` prompt branches. `APIPI_PLATFORM_PROMPT`, `APIPI_PLATFORM_PROMPT_ADDITIONAL`, `APIPI_PI_SYSTEM_PROMPT`, and `metadata["apipi.system_prompt"]` are unchanged.
- Small cleanups: Playwright MCP checks are removed (`mcp_playwright_*`, `/opt/apipi/playwright-mcp`, `APIPI_SANDBOX_AUTO_PLAYWRIGHT`, `[sandbox.browser]` keys). Stdio MCP helpers (`tests/support/mcp_stdio*.py`) are removed; MCP is HTTP only. `APIPI_WORKSPACE_TTL` / `workspace_ttl` is removed (use `APIPI_SANDBOX_TTL_OPENAI_HOSTED`). Flat TOML keys no longer warn (use `[pi]`, `[sandbox]`, `[mcp]` tables). `host` / `jail` run modes fail with the generic run-mode message. `worker_memory_mb` defaults in the validator; `node_memory_mb()` assumes it is set. The Pi session cache keeps only `pi_session_id` (migration `0024` drops `sessions.pi_session_uri`). Dead code is removed (`identity_from_result`, `get_db`, `env_spec_or_none`, `StaticBearerAuth`, `ImageStore` base, `part_name`, `allowed_egress_ips`, `pi_binary`, `require_size_rootfs`, the `Execution` protocol, and package re-exports). Test-only helpers (`sweep_host_orphans`, `get_item`, `artifact_blob_path`, `split_bytes`, `verify_checksums`) are removed with their tests. `artifact_blob_uri` / `read_blob_uri` are removed now that the session cache keeps only `pi_session_id`.
- Worker protocol v2 is a hard cut. The first worker message must be `register` with `protocol: 2`; anything else closes the socket with code `1008` and reason `unsupported_protocol`. The API answers with `hello.reply` (`protocol`, `worker_id`, `generation`, `sessions: {id: last_seq}`). Envelope and message schemas live in `src/apipi/worker/protocol.py`, shared by the API and the worker. Upgrade the API and all workers together. Rejections are logged and counted in `apipi_worker_protocol_total`. See [ADR 0015](https://github.com/GEKI-AI/apipi/blob/main/specs/decisions/0015-worker-protocol-v2.md) and [workers](docs/workers.md).
- The shared `APIPI_WORKER_TOKEN` is removed with no fallback. Setting it fails API and worker startup with a message that points at `apipi workers token create`. Migration: run `apipi migrate` (migration `0025` adds the `worker_tokens` table), create one token per worker with `apipi workers token create --name ...` (the secret prints once, starts with `apipi_wk_`, and only its SHA-256 hash is stored), write each secret to a file on its worker host, and set `APIPI_WORKER_TOKEN_FILE` to that path. A token is bound to one `worker_id` on first register (or with `--worker-id` at creation); a register without `id` is assigned the bound id, and a different explicit `id` is rejected with `token_bound`. A token is valid only on `/internal/worker` (`401` everywhere else, recognised by prefix without a database lookup), and several active tokens per worker allow rotation without downtime (`create`, roll out, `revoke`; revocation closes live sockets). See [workers](docs/workers.md) and [production](docs/production.md#worker-token-rotation).
- Workers no longer take `DATABASE_URL`. `apipi worker` refuses to start when `DATABASE_URL` is set in its environment: unset it on worker hosts, since only the API connects to Postgres. The worker also needs no object-store credentials (artifacts move through API-issued presigned URLs or the shared store root). `prepare_worker` no longer warns about an unset vault master key. Migration: remove `DATABASE_URL` from the worker environment (systemd unit, Compose service, or shell profile) and point the worker at the API with `APIPI_API_URL` plus its token file.
- Worker sockets require TLS for non-loopback API URLs. `apipi worker` fails at startup when `APIPI_API_URL` is a non-loopback plain `http://` or `ws://` address: serve the API behind `https://` (or `wss://`) with a trusted certificate. Loopback `http://` URLs stay allowed for local development. Migration: change the worker `APIPI_API_URL` to the `https://` API endpoint before upgrading. Optional mutual TLS (`APIPI_WORKER_CLIENT_CERT` / `APIPI_WORKER_CLIENT_KEY`, plus `APIPI_WORKER_SERVER_CA` for a private API CA) is new and off by default. See [workers](docs/workers.md#transport-security) and [production](docs/production.md#worker-transport-security).
- `environment.type=none` tool and metadata checks now apply to every `type=none` session, not only chat sessions. Built-in tools are always off (`apipi.builtin_tools=on` is `400` with code `builtin_tools`), `apipi.codemode` `on` or `only` is `400` with code `builtin_tools`, and only function tools and HTTP MCP with `server_url` are allowed (anything else is `400` with code `tool_not_allowed`, renamed from `chat_tool`; update clients that match `chat_tool`). The checks run on agent create and update (when the agent's `session_defaults.environment.type` is `none`), on session create (using the effective environment, tools, and metadata, so a hosted-saved agent that conflicts with `type=none` cannot be used for one), on session update (whenever metadata changes on a `type=none` session), and on template import. `apipi.codemode` `on` or `only` together with an effective `apipi.builtin_tools=off` is `400` with code `builtin_tools` everywhere. Template import also stops injecting empty `skills`/`files` lists into `session_defaults.environment`, so `type=none` defaults validate instead of failing on hosted-only fields.

### Security

- MCP headers no longer expand `${ENV}` from the gateway process.
  Values that contain `${...}` are rejected at session create, so a
  tenant can no longer have gateway environment values (for example
  `APIPI_VAULT_MASTER_KEY` or `DATABASE_URL`) sent to a server of
  their choice. Store MCP secrets in a vault credential
  (`static_bearer` bound to `mcp_server_url`) and attach `vault_ids`
  on the session. `examples/tavily.yaml` now shows that shape.
- MCP `server_url` targets pass an SSRF guard on the gateway connect
  and on every host broker call. Loopback, RFC 1918, link-local
  (including cloud metadata), CGNAT, ULA, multicast, reserved, and
  other special-use addresses are rejected, including hostnames that
  resolve to them. Operators with an MCP server on a private address
  (including local development on `127.0.0.1`) list it in the new
  `APIPI_MCP_ALLOW_HOSTS` / `[mcp].allow_hosts` setting (hostnames or
  CIDRs, empty by default).

### Upgrade notes

- Replace `${ENV}` in MCP tool headers with a vault credential and
  `vault_ids` on the session. Sessions that still send `${...}`
  headers fail with a message that names the vault path.
- MCP servers on private addresses need `APIPI_MCP_ALLOW_HOSTS` /
  `[mcp].allow_hosts`, or session create fails with a blocked-host
  message.

## [0.13.0] - 2026-09-30

### Added

- Gateway auth is bounded and revocable. `APIPI_AUTH_CACHE_MAX`
  (default `10000`, `0` disables caching) adds LRU eviction to the
  auth cache. `Gateway.invalidate_auth`,
  `Gateway.invalidate_auth_where`, and `Gateway.clear_auth_cache`
  drop entries in process, and
  `POST /v1/apipi/auth/invalidate` drops the caller's tenant entries
  over HTTP. The auth plugin may be `async def` or sync (sync runs in
  a worker thread), concurrent misses for one key call the plugin
  once, and the tenant lookup is memoized.
- Optional `APIPI_AUTHORIZE` hook (`authorize=` on `Gateway.create`)
  enforces resource-level authorization with stable action names
  (`agent.read`, `agent.write`, `agent.list`, `agent.run`,
  `session.read`, `session.list`, `vault.*`, `file.*`, `skill.*`,
  `template.*`, `usage.read`, `auth.invalidate`). List actions accept
  `AuthFilter(ids)`. Missing resources stay `404`; denied resources
  are `403`. Existing sync `authenticate` plugins now run in a
  thread; plugins that relied on running on the event loop thread
  should become `async def`.
- Model-host attribution. The per-session broker stamps
  `x-apipi-session-id`, `x-apipi-turn-id`, `x-apipi-agent-id`, and
  `x-apipi-agent-revision` on every model request and strips
  guest-set `x-apipi-*` headers. Agents gain a read-only `revision`
  (`1` on create, plus one per update and restore). New
  `APIPI_MODEL_ATTRIBUTION_HEADERS` (default `true`) controls
  stamping.

### Breaking

- Pi 0.99.1 (was 0.85.1). All guest images are rebuilt with a new
  store version; operators must mirror and pull again.
- MCP runs on Pi's built-in MCP client over streamable HTTP.
  Model-facing tool names change from `mcp_<server_label>_<tool>` to
  `mcp__<server_label>__<tool>`, sanitised and hashed when long. API
  output items still carry `server_label` and the original tool name.
  `server_label` must match `[A-Za-z0-9_-]`. New `mcp_list_tools`
  output items list each server's tools at turn start. New
  `allowed_tools` support restricts the tools. Other
  `require_approval` values, `connector_id`, and `authorization` are
  `not_implemented`.
- MCP tool input is the flat OpenAI format only: `server_url` at top
  level, plus `allowed_tools`, `require_approval` (only `never`), and
  `server_description`. The nested `transport: {...}` shape is removed
  and returns `unknown_field`. Stored agents and sessions with the
  nested shape must be recreated; using them fails with a clear error.
- stdio MCP is removed. `transport: {type: "stdio"}` tools are no
  longer accepted, and `stdio_on_host` is gone from the isolation
  plugin interface.
- A slash-command input (for example `/mcp`) that Pi handles as a
  command ends the turn with an error instead of hanging.
- New opt-in `apipi.codemode` (`off` | `on` | `only`, default `off`).
- Pi starts with an explicit extension list and `PI_OFFLINE=1`.
  Workspace `.pi/` project resources are never loaded.

## [0.12.1] - 2026-09-30

- The official image publish is x86_64 `default` and `browser` only.
  It does not build `work` or aarch64. Build those locally if you need
  them.

### Fixed

- The aarch64 Node tarball pin matches nodejs.org. A local aarch64
  image build no longer fails the checksum.
- The Images workflow runs only on a release tag. Dispatch it with
  `--ref vX.Y.Z`. A branch dispatch fails before it builds or signs.
- `apipi images verify` and `apipi images mirror` accept
  `--signer-identity` and `--signer-issuer` for a store that was not
  signed by the tag. The 0.12.0 store was signed from `main`.
- A release that does not change image inputs reuses the previous
  store and signs it for the new tag. It does not rebuild the guest.
- `APIPI_SANDBOX_IMAGE_MIN_VCPUS` overrides the per-image vCPU floor.
  Unset still uses the recipe or manifest value. A value below the
  recommended floor logs a warning.
- A log flush failure (for example a closed test capture stream) no
  longer propagates out of the logging handler, so it cannot kill the
  request or socket handler that logged.

## [0.12.0] - 2026-09-30

The 0.12.0 image store was not signed by the release tag. Use 0.12.1
for official images.

### Breaking

- The image store is versioned per release. `image_source` is the base
  above `v<version>/` prefixes. Workers pull that store version, which
  defaults to the running ApiPi version, instead of `latest` per image.
  Set `APIPI_IMAGE_STORE_VERSION` to pin or roll back. The official
  store is the GitHub release at
  `https://github.com/GEKI-AI/apipi/releases/download/v<version>/`.
- Operators mirror and verify that store, then pull. `apipi images
  push` without `--store-version` writes a deprecated schema 1 store
  and warns. `--force` is refused for a complete versioned prefix.
- The local kernel layout gains `kernels/<arch>/<kernel_version>/vmlinux`.
  `kernels/<arch>/vmlinux` remains a compatibility copy.
- All guest images (`default`, `work`, `browser`) now use Debian
  trixie slim instead of Alpine. Operators must re-pull or rebuild
  every image. 0.11.x images and manifests are not compatible.
- Alpine support is removed from the image build. There is no
  `ALPINE_VER`, minirootfs, or `apk`. Custom recipes must use Debian
  package names in `PACKAGES`.
- Environment `packages.system` now means Debian/apt package names.
  Alpine names such as `py3-*` and `font-*` no longer work. The
  restricted-network egress allowlist for system packages uses the
  Debian mirrors.
- Guest Python is 3.13. Node comes from a pinned nodejs.org tarball.
  The guest uses glibc. `ripgrep` (`rg`) is in every image.
- Images are larger. Check rootfs disk space on workers.
- The `browser` image is rebuilt on agent-browser and
  chrome-headless-shell, and is x86_64 only. An aarch64 worker does
  not offer it. Size `L` or `image: browser` on aarch64 is
  `image_unavailable`. `default` and `work` stay multi-arch.
- Playwright MCP is removed. `mcp_playwright_*` tools no longer exist.
  Use bash and `agent-browser` through the built-in `browser` skill.
- `APIPI_SANDBOX_AUTO_PLAYWRIGHT` and `[sandbox.browser]` are warned
  about and ignored.
- Prompt overrides `APIPI_PLATFORM_PLAYWRIGHT`,
  `APIPI_PLATFORM_PLAYWRIGHT_FILE`, `APIPI_PLATFORM_CHROMIUM`, and
  `APIPI_PLATFORM_CHROMIUM_FILE` are removed.
- Browser VMs get at least 2 vCPUs, including size `M`.
- The browser no longer writes into `/workspace/outputs` by default.
  Only artefacts the user explicitly asked for go there.
- `examples/playwright.yaml` is removed. Browser examples use
  agent-browser.

## [0.11.0] - 2026-09-29

### Breaking

- Agent versions are explicit snapshots. Create and update no longer
  write a version. `active_version` is gone from agent responses.
  `agent_version` is gone from session, turn, usage, and lifecycle
  bodies. `metadata["apipi.agent_version"]` is ordinary metadata.
  `POST /v1/apipi/agents/{id}/versions/{version}/activate` is removed.
  Version create no longer accepts `definition` or `activate`, and
  versions have no `status`. `note` is replaced by `name` and `comment`.
  Sessions follow the live agent again, so an edit changes the next turn
  of a conversation that already exists. See
  [agent versions](docs/agent-versions.md).
- A database that already applied the 0.10.x `0019_agent_versions`
  migration must be recreated. That revision was rewritten in place.

### Added

- `POST /v1/apipi/agents/{id}/versions/{version}/restore` copies a
  snapshot back onto the agent and first saves the live definition as a
  `pre_restore` snapshot.

### Changed

- `APIPI_AGENT_VERSIONS_KEEP` defaults to 10.

### Fixed

- Registry `thinking_levels` follows Pi's map: a missing standard
  level is allowed, `null` is unsupported, and `xhigh` or `max` need
  an explicit string. A missing `models.json` still includes the
  session model and registry capabilities. `service_tier` of `null` or
  `auto` is ignored.
- An agent update that sends only `reasoning` keeps the other metadata
  keys. A session update can change `reasoning.effort`, including back
  to the agent or model default. A 400 for a disagreeing level happens
  only when the same request sets both `reasoning.effort` and
  `metadata["apipi.thinking"]`.

## [0.10.1] - 2026-09-29

### Fixed

- The wheel build no longer lists `src/apipi/worker/pi/prompts` in
  `force-include`. Those files are already in the package, and the
  duplicate stopped the 0.10.0 publish.

## [0.10.0] - 2026-09-29

### Added

- Agent versions. A session keeps the version it was created with.
  Move a session by setting `metadata["apipi.agent_version"]` while it
  is idle. See [agent versions](docs/agent-versions.md).

### Changed

- An agent edit no longer changes sessions that already exist.
- Guest kernel is Linux 6.1.186 from the Firecracker 1.17 CI set,
  with virtio-rng. The old quickstart 4.14 kernel is no longer
  downloaded.
- Browser sessions pass Chromium launch flags that skip first-run
  network, and a 30 second navigation timeout. Guest `mcp:` lines are
  logged at info, including a timed-out tool name and duration.
- Pi prompt text ships as files under `src/apipi/worker/pi/prompts/`
  and is read at startup. The identity line names Pi as the harness.
  `APIPI_PLATFORM_NAME` is that name. The hosted prompt no longer states
  a sandbox timeout. User-provided files under `inputs/` are restored.
  Other workspace files, including `outputs/`, are not.

### Fixed

- `apipi images check browser` mounts `/dev` before it creates `shm`
  and `pts`, so the read-only rootfs check no longer fails with
  "Read-only file system".

## [0.9.0] - 2026-09-29

### Added

- Every guest image includes `pip` and a pinned static `uv`. `pip`
  installs into the workspace user site. `uv` cache stays on `/tmp`.
- Guest image `work` adds Excel, Word, PowerPoint, PDF, CSV, and chart
  libraries. It needs sandbox size `M` or larger. It does not include
  LibreOffice or pandoc.
- Session input accepts `input_image` data URLs and a list of messages.
  A model capability registry tells Pi which models accept images.
- Agents and sessions accept OpenAI `reasoning.effort`. It maps to Pi
  thinking and is mirrored as `apipi.thinking`.
- Operator prompt fragments can be overridden per environment type, or
  from a file. `${platform_name}` replaces the built-in name. Extend
  mode replaces only Pi's intro.
- The built-in hosted prompt now says an idle or TTL stop deletes the
  workspace, and names `inputs/` and `outputs/`. Hosted sessions get a
  capability block for image, size, RAM, vCPUs, and network. It does
  not claim a browser is available.

### Documentation

- A replacement system prompt keeps the platform blocks, instructions,
  context files, and skills. It drops Pi's tool list and all tool
  guidelines, including MCP and Playwright guidance. Context files
  (`AGENTS.md` and the other names Pi loads) are documented as a
  supported way to add prompt text.

## [0.8.0] - 2026-09-29

### Breaking

- Public session responses no longer include `environment.directory`.
  The host path stays in the store for the worker.
- TOML keys removed in 0.7.0 are now unknown settings:
  `thinking_summary`, `auto_title`, `sidekick_model`,
  `sidekick_base_url`, and `sidekick_api_key`. They were ignored in
  0.7.0.

### Added

- ApiPi-only routes are canonical under `/v1/apipi/`. The old paths
  remain as deprecated aliases and are logged once per process.
  OpenAI `container_size` (`small` / `medium` / `large`) maps to
  `sandbox_size`.
- Hosted sessions expose sandbox runtime status. Cold boot emits
  `agent.session.environment.pending` and `environment.connected`.
  Every stop emits `environment.disconnected` with a reason. GET
  session includes `environment.status` and `environment.sandbox`.
  `GET /v1/agents/environments/{id}` returns that status. Eager boot
  is off unless `APIPI_SANDBOX_EAGER_BOOT` or
  `metadata["apipi.sandbox_eager_boot"]` is set.
- `apipi images check browser` is a local developer check for the
  browser image. It is not part of CI.

### Changed

- Browser and image hints are based on the resolved computer and on
  tools that actually registered, not on sandbox size. Sessions
  without a sandbox no longer get size text.
- The browser image adds Noto CJK and emoji fonts. Size `L` defaults
  to 2 vCPUs (`APIPI_SANDBOX_L_VCPUS`).

### Removed

- Unused `sandbox_playwright_mcp` / `APIPI_SANDBOX_PLAYWRIGHT_MCP`.
  The TOML key `playwright_mcp` is ignored. The dead browser hint in
  the platform prompt is gone. Playwright guidance is added only after
  those tools register.

### Fixed

- Stdio MCP servers attach again. The Pi extension now writes
  newline-delimited JSON. It wrote `Content-Length` frames, which MCP
  servers ignore. Startup no longer waits about 5 seconds per server.
- Guests mount `/dev/shm` and `/dev/pts`.
- HTTP MCP tools reach Pi. The extension lists them from the host
  credential broker URL and registers `mcp_<server_label>_<tool>`.
  The guest still does not receive the bearer. A later list failure
  is logged and the turn continues.
- A warm sandbox attach is not blocked by another session's cold boot.
- `GET` during a remote turn no longer marks the turn interrupted while
  the worker lease is live.
- Host setup runs off the event loop.
- A pool stop sends `lease.release` so idle sessions do not keep worker
  capacity. The worker handles `lease.revoke`.

## [0.7.0] - 2026-09-28

### Breaking

- Thinking summaries and automatic session titles are removed. ApiPi
  no longer emits `agent.session.turn.thinking.summary.completed`,
  `agent.session.turn.thinking.summary.failed`, or
  `agent.session.title.updated`. Titles are a client concern.
- Settings removed: `APIPI_THINKING_SUMMARY` / `thinking_summary`,
  `APIPI_AUTO_TITLE` / `auto_title`, `APIPI_SIDEKICK_MODEL` /
  `sidekick_model`, `APIPI_SIDEKICK_BASE_URL` / `sidekick_base_url`,
  and `APIPI_SIDEKICK_API_KEY` / `sidekick_api_key`. Those environment
  variables are ignored. The same TOML keys log
  `was removed in 0.7.0 and is ignored` in this release and become an
  `unknown setting` error in the next release.
- `AuthIdentity` no longer has `thinking_summary` or `auto_title`.
  A callback dict that still includes those keys is accepted; the
  keys are ignored.
- `SessionService.create` and `post_event` no longer take
  `thinking_summary` or `auto_title`.
- `apipi.title` and `apipi.title_status` are no longer written or
  interpreted. A metadata update is a plain replace, so leaving those
  keys out removes them.

## [0.6.2] - 2026-09-28

### Added

- Model-call retry and timeout settings (`APIPI_MODEL_RETRY_ENABLED`,
  `APIPI_MODEL_MAX_RETRIES`, `APIPI_MODEL_BACKOFF_BASE_MS`,
  `APIPI_MODEL_BACKOFF_MAX_MS`, `APIPI_MODEL_TIMEOUT_MS`,
  `APIPI_MODEL_PROVIDER_RETRIES`, `APIPI_MODEL_RETRY_AFTER_MAX_MS`).
  ApiPi writes them into Pi `settings.json`. Only Pi retries. See
  [Pi](docs/config.md#pi).
- `agent.session.turn.retrying` and
  `agent.session.turn.retry.completed` while Pi waits to retry a
  model call.
- `upstream_attempts` on a failed turn, the session error, the
  `turn.failed` log, the turn log, and the usage row.
- Turn failures carry `failure_source`, a specific `code`,
  `upstream_status`, and `retryable`. `agent.session.turn.failed`,
  the turn log, and the usage event use the specific code.
  `agent.session.error` and the non-stream `502` body keep
  `model_host_error` for upstream failures in this release, and put
  the specific code in `detail_code`. Set `APIPI_ERROR_CODES=specific`
  to use the specific code on those two surfaces now. The next minor
  release will make `specific` the default and keep
  `legacy_code: model_host_error` for one release after that. See
  [failure codes](docs/errors.md).

### Changed

- A turn no longer fails on the first model error when Pi will retry.
  The failure is classified after the last attempt.
- A `504` whose body mentions a timeout is `upstream_timeout`.
- A turn that exceeds `turn_timeout` fails with `turn_timeout` instead
  of looking like a user cancel. A Pi process that dies mid-turn is
  `pi_exited`. A host Pi killed for `APIPI_PI_MEM_MIB` is `pi_memory`.
  Both still report `model_host_error` on `agent.session.error` until
  the next minor release.
- `turn.failed` and `worker.command.failed` log upstream `429` and
  caller errors at warning. Internal failures and upstream `5xx`,
  timeouts, and connection errors stay at error. A user cancel stays
  info.

## [0.6.1] - 2026-09-27

### Added

- Session lifecycle export. The pool owner emits `session.live.start`,
  `session.live.stop`, and `session.live.heartbeat` when
  `APIPI_LIFECYCLE_EXPORT_URL` or `APIPI_LIFECYCLE_SINKS` is set.
  Events carry environment, image id, version, and digest so usage can
  be split by sandbox image. Off by default. See
  [usage](docs/usage.md#session-lifecycle-export).
- Auth callbacks may return `org_id`. Session create stores it and
  returns it on the session. It does not change list or get scope.

### Changed

- `apipi_pi_kill_total` now uses `crash` when a Pi process exits by
  itself, and `drain` when a worker drain kills sessions that are not
  in a turn. Drain previously incremented `idle`.

## [0.6.0] - 2026-09-27

### Added

- Agent templates. `POST /v1/templates` stores a zip of an agent's
  configuration. Import, download, and create a new agent from that
  zip. Secrets and credential values are not included. See
  [agent templates](docs/agent-templates.md).
- Agents accept `session_defaults` (`environment` and `vault_ids`).
  Session create inherits those values unless the request overrides
  them. `inherit_agent_defaults: false` skips them.
  `metadata["apipi.sandbox_size"]` and `metadata["apipi.sandbox_image"]`
  are aliases of the defaults and stay accepted.
- `APIPI_MODEL_LIST=probe|turn|off` (default `probe`). Agent create
  and model edit check the list. Turns do not. `off` never calls
  `/models` and uses `APIPI_MODELS`.

### Fixed

- A missing `agent.model` fails the session instead of hanging.
  If the host later rejects the selected model, the turn fails with
  `model_host_error`. Worker turn tasks log that failure, including
  4xx, instead of leaving an unretrieved exception.

## [0.5.3] - 2026-09-26

### Added

- `apipi images push` uploads the newest local guest image build.
  `apipi images publish` is the same command. `--to` defaults to
  `APIPI_IMAGE_SOURCE`. An identical image already in the store is
  skipped instead of failing. `--force` uploads again.
- Guest image S3 settings `APIPI_IMAGE_S3_ENDPOINT`,
  `APIPI_IMAGE_S3_REGION`, and `APIPI_IMAGE_S3_ADDRESSING`, plus
  env-only credentials `APIPI_IMAGE_S3_ACCESS_KEY_ID`,
  `APIPI_IMAGE_S3_SECRET_ACCESS_KEY`, and `APIPI_IMAGE_S3_PROFILE`.
  Unset values fall back to `APIPI_S3_*` and the default AWS
  credential chain. The artifact store is unchanged.

### Fixed

- Pushing guest images to a missing S3 bucket fails with a clear
  error instead of starting a new index.
- S3 guest image pull streams the blob to disk. Peak RAM no longer
  grows with image size. The kernel download uses the same path.

## [0.5.2] - 2026-09-25

### Added

- Auth plugins may take an `AuthRequest` (`method`, `path`, headers)
  and a `cache_key` so one organization bearer can identify end users
  without sharing a cache entry. Session create stores `user_id` when
  the identity has one. List, get, update, delete, and resume then
  match that user. Identities without `user_id` stay tenant-scoped.

### Changed

- Agent create and update validate `metadata["apipi.sandbox_size"]` and
  `metadata["apipi.sandbox_image"]`. A bad size, an unknown image, or a
  size below the image minimum is `400`. Worker availability is still a
  session placement error. Docs now say the image selects the rootfs,
  with size `L` mapping to `browser` when the image is omitted.

### Fixed

- Browser MicroVM cold start no longer runs `npx -y @playwright/mcp@latest`.
  The browser image vendors a pinned server, and auto-inject starts it
  with `node` at a fixed path. Attach soft-fails after 15 seconds so
  the first turn is not blocked for minutes. The platform prompt does
  not name Playwright MCP tools; those names are registered only after
  attach succeeds. Worker logs include the full `extension_error`
  message. Rebuild with `apipi install --microvm --image browser`.

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
