# Standing orders

Before code:

1. `CONSTITUTION.md`
2. `docs/index.md`
3. `docs/decisions/`
4. The spec for the surface you are changing
5. `docs/process.md` if you are changing a decision

If it is not in a spec, update the spec first or refuse. Do not build
items from `docs/roadmap.md` unless the spec has moved.

## Stack

Python 3.13, FastAPI, Postgres. uv only. Pi via RPC, one process per
session. Run mode `APIPI_RUN_MODE` (`host` \| `jail` \| `microvm`),
default `jail`. Example UI off unless `APIPI_EXAMPLE_UI=1`.
OpenAI-compatible `base_url`. No Node in the gateway.

## Do not

- A second harness in this version
- First-party search or a browser engine in the gateway
- Silent fallback between run modes
- ChatKit, workflow canvases, `/v1/runners`
- Pi types in HTTP
- Pi JSONL as the database
- Accept `multi_agent` silently
- Tenant keys in the browser
- Rewrite the event log
- A custom docs frontend
- A superseded ADR file
- pip, `python -m venv`, or pre-commit hooks
- MkDocs or docs deps in the apipi package

## Do

- Tenant-scope every query
- Persist the public event before SSE
- Fail unknown OpenAI fields clearly
- Pin Pi when touching the adapter
- Warn at startup when run mode is `host`
- Idle Pi TTL default 15 minutes (`APIPI_IDLE_TTL`)
- uv for all Python (`uv sync`, `uv run`, `uv lock`)
- Before commit: format, lint, typecheck, tests (see `CONTRIBUTING.md`).
  Run them locally. Do not commit until they pass.

## Slices

After a slice lands, re-read `CONSTITUTION.md`, `docs/index.md`, the
spec for the next surface, and the current code. Do not rely on
memory from the previous slice.

## Issues

Write issues so a human can read them in one pass. Goal, acceptance,
spec paths. Do not list non-goals. Anything not in the goal is out of
scope. Mention a non-goal only when someone would reasonably think it
was included.

## Git

`CONTRIBUTING.md`. Issue first. Branch `issue-N-slug` from `main`. One
issue per PR. Human review, squash merge. Do not commit red.
