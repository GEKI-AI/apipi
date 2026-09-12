# How we work

Specs describe the product as it is. Code follows. If they disagree, fix
the spec or the code in the same change.

Work that is not in this version lives in [roadmap.md](roadmap.md), not
on the spec pages.

| Kind | Where |
| --- | --- |
| Rules | `CONSTITUTION.md` |
| Agent rules | `AGENTS.md` |
| Specs | `docs/*.md` |
| Decisions | `docs/decisions/*.md` |
| Tests | `tests/` |
| Later | `docs/roadmap.md` |
| Agent steps | `.agents/skills/` |
| Checks | `scripts/check` |

Name, license, docs tools, git, and short files that point at
`AGENTS.md` are repo facts (`README.md`, `LICENSE`, `mkdocs.yml`,
`requirements-docs.txt`, `CONTRIBUTING.md`, `pyproject.toml`,
`CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`). They do
not get decision files.

When the constitution changes, update `AGENTS.md` in the same change.

Write docs, issues, and pull requests in simple technical English. See
`CONTRIBUTING.md`.

## Change a decision

Edit the decision file and the specs in the same change. Do not keep an
old copy. Do not add a decision file for something that is just a fact
of the repo.

Small copy or event-name fixes do not need a decision file.

## Docs site

Separate from the apipi package. `requirements-docs.txt`, not
`pyproject.toml`.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

Markdown in git is the source. Do not add a custom docs app.

## Tests

Pytest. Specs say what is true. Tests check the public API and the
constitution, not Pi internals. Mock Pi RPC. Same change as the code.
Checks before commit are policy, not git hooks. See `CONTRIBUTING.md`.

Compatibility: HTTP tests in `tests/api/test_compat.py` against
[api.md](api.md). Each yes row has a named test. Fast e2e runs in CI
(`pytest -m "not slow"`). A slow check against the official OpenAI
Python client (`beta.agents`) is local only and skips if the SDK is not
installed.

## Contribute

Issues are the usual list of work. A PR without an issue is fine when
the change was asked directly. See `CONTRIBUTING.md` at the repo root.

## Agents

Rules: [agents.md](agents.md) (`AGENTS.md` at the repo root).
Steps: `.agents/skills/` (`plan`, `create-issue`, `work-issue`,
`review`). Do not copy those into `AGENTS.md`.

Do not add features that are not in a spec. Do not implement the roadmap
unless the spec has moved.

An issue is common, not required. One checkout is one branch. Two
changes at the same time must not edit the same files. Use another
folder (`git worktree`) or another session.
