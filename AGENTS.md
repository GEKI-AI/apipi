# Agent rules

Laws: `CONSTITUTION.md`. Product: `docs/`. How we work:
`CONTRIBUTING.md`. If it is not on a product page, update that page in
this change or stop. Do not build `docs/roadmap.md` unless a product
page has moved.

When the constitution changes, update this file in the same change.

## Writing

Use simple technical English and common words. Do not invent names for
things that already have names. Do not invent features.

Docs, README, and the MkDocs site use complete sentences and enough
explanation that a new reader can set up and use the API without
guessing. Short choppy fragments are wrong for docs. Tables are still
good for endpoints, env vars, and fields.

Issues can be as long as they need to be. Include known
implementation details. Do not squeeze the goal. Pull request bodies
and commit messages stay short.

Code comments stay omitted unless asked.

## Map

| Path | What |
| --- | --- |
| `src/apipi/api/` | HTTP routes |
| `src/apipi/schemas.py` | Public types |
| `src/apipi/store/` | Durable store |
| `src/apipi/pi/` | Harness adapter |
| `src/apipi/auth.py` | Auth callback |
| `docs/` | Product and operator docs. Read the page for the part you are changing. |
| `specs/decisions/` | ADRs. Read when the architecture changes. |
| `tests/api/` | Public HTTP |
| `tests/unit/` | Internals, mocks |
| `tests/e2e/` | Live Pi |
| `tests/support/` | FakeHarness, fakes |

Before code, read the `docs/` page for the part you are changing.

## Stack

Python 3.13, FastAPI, SQLite (one process) or Postgres (shared
store). uv
only. PyPI name `geki-apipi`;
import and CLI `apipi`. Pi via RPC, one process per
session. Run mode `APIPI_RUN_MODE` (`none` \| `microvm`, or
`package.mod:Class`). Process default `none`. Production
SaaS/enterprise is `microvm`. `none` is local/dev only. `microvm`
probes a real sandbox before the API listens, then exits if it cannot
start. Custom backends that set `needs_probe` do the same. No silent
fallback. `host` and `jail` are not valid. OpenAI-compatible
`base_url`. No Node in the gateway.

## Do not

- A second harness in this version
- First-party search or a browser engine in the gateway
- Silent fallback between run modes
- ChatKit, workflow canvases, `/v1/runners`
- Pi types in HTTP
- Pi JSONL as the database
- Accept `multi_agent` silently
- Tenant keys in the browser or in Postgres
- Rewrite the event log
- A custom docs frontend
- An old copy of a decision file
- pip, `python -m venv`, or pre-commit hooks
- MkDocs or docs deps in the apipi package

## Do

- Change `CONSTITUTION.md` rarely. Edit in place. No amendment log.
- Tenant-scope every query. Auth is a callback; do not store keys.
- Persist the public event before SSE
- Fail unknown OpenAI fields clearly
- Pin Pi when touching the adapter
- Warn at startup when run mode is `none`
- Idle Pi TTL default 15 minutes (`APIPI_IDLE_TTL`)
- uv for all Python (`uv sync`, `uv run`, `uv lock`)
- Before commit: `./scripts/check`. Add `--docs` if docs changed.
  Do not commit if checks fail. GitHub CI is fast (`pytest -m "not slow"`).

## Workflows

How to plan, file issues, implement, review, and release lives in
`.agents/skills/`. Read those files. Do not copy them here.

| Skill | When |
| --- | --- |
| `plan` | Plan work and file issues |
| `create-issue` | File one GitHub issue |
| `work-issue` | Do one change and open a PR |
| `review` | Review a PR |
| `release` | Tag `origin/main` for PyPI and a GitHub Release |

An issue is common, not required. One checkout is one branch.
Parallel work is best effort when files do not overlap. Use another
folder (`git worktree`) or another session.

## Git

`CONTRIBUTING.md`. Short-lived branch from `main` (`issue-<n>-<slug>`,
or a short name if there is no issue). One change per PR. Rebase,
squash, delete the branch. Commit when asked to implement, commit, or
open a PR. Merge only when the user asks to merge. Default: return the
PR URL. Never force-push `main`.
