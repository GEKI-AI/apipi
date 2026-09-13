# Contributing

The usual path is a GitHub issue, then one change on a short-lived
branch. A pull request without an issue is fine when the work was asked
directly and is already small.

Read `CONSTITUTION.md`, `docs/process.md`, and `AGENTS.md`. If it is
not on a product page under `docs/`, it is not in this version.

Agent steps: `.agents/skills/`.

## Writing

Use simple technical English and common words. Do not invent names for
things that already have names. Do not invent features.

Docs, specs, README, and the MkDocs site use complete sentences and
enough explanation that a new reader can set up and use the API without
guessing. Short choppy fragments are wrong for docs. Tables are still
good for endpoints, env vars, and fields.

Issues can be as long as they need to be. Include the goal, what to
check when done, spec paths, and any implementation details already
known. Do not squeeze the goal. Pull request bodies and commit
messages stay short.

Code comments stay omitted unless asked.

## Issues

[Open an issue](https://github.com/GEKI-AI/apipi/issues). That is the
list of planned work.

Write it so a human and an agent can do the work without a meeting.
Use as much text as that takes.

| Kind | Include |
| --- | --- |
| Request | Goal, what to check when done, product page paths. Known implementation details when you have them. |
| Bug | What happens, what should happen, product page path, how to reproduce |

Optional: **Details** (how to implement, if already known), **Files**
(what this change will edit), **Blocked by** (issue that must land
first). Use Files and Blocked by when splitting work so two people or
agents can work at the same time.

Do not list what this is not. Anything not in the goal is out of scope.

Do not file work that lives on `docs/roadmap.md` unless a product page
has moved.

Keep each change small. One goal, few files. If two changes would edit
the same file, land shared code first.

## Git

PRs only onto `main`. Short-lived branches. Squash merge. No merge
commits on `main`.

| | |
| --- | --- |
| Branch | `issue-<n>-<slug>` from latest `main`, or a short name if there is no issue |
| Scope | One change per branch and per pull request |
| Merge | Squash onto `main`, then delete the branch |
| Record | The squash message is the history. In-progress agent commits are not |

No `develop` branch. No force-push to `main`. Rebase onto `origin/main`.
Do not merge `main` into the branch.

One checkout is one branch. Two changes at the same time need another
folder (`git worktree`) or session, and must not edit the same files.

Commit when asked to implement, commit, or open a PR. Merge only when
asked to merge. Default after a PR: return the URL.

## Pull requests

1. If there is an issue, the PR body starts with `Fixes #N`.
2. Spec and code change together when they disagree.
3. GitHub checks pass (`Check`, `Tests`, and `Docs`).
4. Review, then squash-merge.

## Reviewing a PR

1. Only this change's goal?
2. Spec and code agree?
3. Tenant-scoped? No stored secrets? No Pi types on HTTP?
4. Tests cover what the issue said to check?
5. Any constitution or `AGENTS.md` rule broken?

Must fix = a missed goal, spec and code disagree, or a broken rule.
Small style notes do not block unless they break a spec.

## Tooling

[uv](https://docs.astral.sh/uv/) only. Never pip, never a bare
`python -m venv`. Python 3.13+.

```
uv sync
```

| Tool | What |
| --- | --- |
| uv | Python, deps, commands |
| ruff | Lint and format |
| ty | Types |
| pytest | Tests |

No git hooks. Run checks yourself. CI runs them on the PR.

## Tests

Pytest in `tests/`. Specs say what is true. Tests land in the same
change as the code.

| We test | We do not |
| --- | --- |
| Public HTTP vs the spec | Pi internals or JSONL |
| Tenant isolation (wrong tenant is 404) | The agent loop |
| Unknown fields fail clearly | Roadmap features |
| Event log is the transcript | |

Gateway tests mock Pi RPC. Local pytest uses SQLite.

Commands, what each suite contains, and what microvm needs
are in `docs/tests.md`.

GitHub CI runs everything that finishes in a couple of seconds:
format, lint, types, docs, unit tests, and fast e2e
(`pytest -m "not slow"`). No Firecracker on GitHub.

The full suite is local: `./scripts/check` including
`@pytest.mark.slow`. Optional Postgres: `APIPI_TEST_DATABASE_URL`.
Do not add slow jobs to GitHub.

## Before you commit

```
./scripts/check
```

If docs changed, `./scripts/check --docs`. Fast like GitHub:
`./scripts/check --fast`.

Do not commit if checks fail.

GitHub: `Check` (format, lint, types), `Tests` (`pytest -m "not slow"`),
`Docs`.

## Docs site

Separate from the apipi package. Do not add MkDocs to `pyproject.toml`.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

Local Postgres for `apipi migrate` / `apipi serve`:

```
docker compose up -d postgres
```

`DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi`.
