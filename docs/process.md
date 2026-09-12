# How we work

Specs describe the product as it is. Code follows. If they disagree, fix
the spec or the code in the same change.

Work that is not in this version lives in [roadmap.md](roadmap.md), not
on the spec pages. Do not implement the roadmap unless the spec has
moved.

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

When the constitution changes, edit `CONSTITUTION.md` in place and
update `AGENTS.md` in the same change. Do not append an amendment log.
Opinion changes belong in the spec that is wrong, not in a new history
file.

## Writing

Use simple technical English and common words. Do not invent names for
things that already have names. Do not invent features.

Docs, specs, README, and the MkDocs site use complete sentences and
enough explanation that a new reader can set up and use the API without
guessing. Short choppy fragments are wrong for docs. Tables are still
good for endpoints, env vars, and fields.

Issues can be as long as they need to be. Include known
implementation details. Do not squeeze the goal. Pull request bodies
and commit messages stay short.

Code comments stay omitted unless asked.

See `CONTRIBUTING.md`.

## Change a decision

Edit the decision file and the specs in the same change. Do not keep an
old copy. When a decision changes, edit the ADR. Law 9 in the
constitution already says this.

Do not add a new ADR for copy, naming, or process nits. Add a new ADR
only when architecture actually changes. Do not add a decision file for
something that is just a fact of the repo.

## Docs site

The MkDocs site is separate from the apipi package. Dependencies live in
`requirements-docs.txt`, not `pyproject.toml`. Markdown in git is the
source. Do not add a custom docs app.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

The home page, install, configuration, the quickstart, and the use
pages (API, auth, environments, tools, usage, architecture) are for
people who want to run the product. Contributing, the constitution,
agent rules, the roadmap, and ADRs live under Contribute.

## Tests

Pytest. Specs say what is true. Tests check the public API and the
constitution, not Pi internals. Mock Pi RPC. Same change as the code.
Checks before commit are policy, not git hooks. See `CONTRIBUTING.md`.

Compatibility: HTTP tests in `tests/api/test_compat.py` against
[api.md](api.md). Each yes row has a named test. Fast e2e runs in CI
(`pytest -m "not slow"`). A slow check against the official OpenAI
Python client (`beta.agents`) is local only and skips if the SDK is not
installed. That check creates a session with an inline agent and
`openai_hosted`, the same fields as `examples/openai_sdk.py`. The
runnable script streams against a live gateway; see
[quickstart](quickstart.md).

## Contribute

Issues are the usual list of work. A PR without an issue is fine when
the change was asked directly. See `CONTRIBUTING.md` at the repo root.

## Agents

Rules: [agents.md](agents.md) (`AGENTS.md` at the repo root).
Steps: `.agents/skills/` (`plan`, `create-issue`, `work-issue`,
`review`). Do not copy those into `AGENTS.md`.

Do not add features that are not in a spec. Do not implement the roadmap
unless the spec has moved.

An issue is common, not required. One checkout is one branch.
Parallel work is best effort when files do not overlap. Use another
folder (`git worktree`) or another session.
