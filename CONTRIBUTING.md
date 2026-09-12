# Contributing

The useful contribution is a detailed GitHub issue. We implement
almost all code with coding agents. A pull request without an issue
is the exception.

Read [CONSTITUTION.md](CONSTITUTION.md), [docs/process.md](docs/process.md),
and [AGENTS.md](AGENTS.md). If it is not in a spec, it is not in this
version.

## Issues

[Open an issue](https://github.com/GEKI-AI/apipi/issues). That is the
work queue.

Write it so a human and an agent can implement without a meeting.

| Kind | Include |
| --- | --- |
| Request | Goal, acceptance checks, spec paths |
| Bug | Observed, expected, spec path, how to reproduce |

Do not list non-goals. Anything not in the goal is out of scope.

Do not file work that lives on [docs/roadmap.md](docs/roadmap.md)
unless the spec has moved.

## Git

`main` is protected. PRs only. Short-lived branches. Squash merge.
Linear history.

| | |
| --- | --- |
| Branch | `issue-<n>-<slug>` from latest `main` |
| Scope | One issue per branch and per pull request |
| Merge | Squash onto `main`, then delete the branch |
| Record | The squash message is the history. Agent WIP commits are not |

No `develop` branch. No merge commits on `main`. No force-push to
`main`.

## Pull requests

1. An issue exists. The PR body starts with `Fixes #N`.
2. Spec and code change together when they disagree.
3. CI is green (`Check`, `Tests`, and `Docs`).
4. A human reviews, then squash-merges.

If you already have a patch, file the issue first and link the patch.
We will often re-implement from the issue and the specs.

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

No git hooks. Checks are policy plus CI, not a trap on every commit.

## Tests

Pytest in `tests/`. Specs are the oracle. Tests land in the same
change as the code.

| We test | We do not |
| --- | --- |
| Public HTTP vs the spec | Pi internals or JSONL |
| Tenant isolation (wrong tenant is 404) | The agent loop |
| Unknown fields fail clearly | Roadmap features |
| Event log is the transcript | |

Gateway tests mock Pi RPC. Local pytest uses SQLite.

GitHub CI runs everything that finishes in a couple of seconds:
format, lint, types, docs, unit tests, and fast e2e
(`pytest -m "not slow"`).

The large validation suite is local: `uv run pytest` including
`@pytest.mark.slow`. Optional Postgres: `APIPI_TEST_DATABASE_URL`.
Do not add slow jobs to GitHub.

## Before you commit

Run the checks. Do not commit red.

```
uv run ruff format src tests
uv run ruff check src tests
uv run ty check src tests
uv run pytest
```

GitHub: `Check` (format, lint, types), `Tests` (`pytest -m "not slow"`),
`Docs`.

## Docs site

Separate from the apipi package. Do not add MkDocs to `pyproject.toml`.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

If you changed docs, also:

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs build --strict
```

Local Postgres for `apipi migrate` / `apipi serve`:

```
docker compose up -d postgres
```

`DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi`.
