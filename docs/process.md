# How we work

Product pages under `docs/` describe the gateway as it is. Code
follows. If they disagree, fix the page or the code in the same
change.

| Kind | Where |
| --- | --- |
| Laws | `CONSTITUTION.md` |
| Agent rules | `AGENTS.md` |
| Product | `docs/` |
| Decisions | `specs/decisions/` |
| Tests | `tests/` |
| Agent steps | `.agents/skills/` |
| Checks | `scripts/check` |

Name, license, docs tools, git, and short files that point at
`AGENTS.md` are repo facts (`README.md`, `LICENSE`, `mkdocs.yml`,
`requirements-docs.txt`, `CONTRIBUTING.md`, `pyproject.toml`,
`CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`). They
need no decision files.

When the constitution changes, edit `CONSTITUTION.md` in place and
update `AGENTS.md` in the same change. Keep a single current file
instead of an amendment log. Opinion changes belong in the product
page that is wrong.

## Writing

Use simple technical English and common words. Prefer names that
already exist. Ship only what a product page describes.

Docs, README, and the MkDocs site use complete sentences and enough
explanation that a new reader can set up and use the API without
guessing. Short choppy fragments are wrong for docs. Tables are still
good for endpoints, env vars, and fields. Tell the reader what to do.
Keep security facts precise.

Issues can be as long as they need to be. Include known
implementation details. Pull request bodies and commit messages stay
short.

Code comments stay omitted unless asked.

See `CONTRIBUTING.md`.

## Change a decision

Edit the decision file and the product pages in the same change.
When a decision changes, edit the ADR in `specs/decisions/`. Law 9 in
the constitution already says this.

Add a new ADR only when architecture actually changes. Repo facts
need no decision file. ADRs stay in git; they are omitted from the
MkDocs site.

## Docs site

The MkDocs site is separate from the apipi package. Dependencies live in
`requirements-docs.txt`. Markdown in `docs/` is the source.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

Home, Get started, and Reference are for people who want to run the
product. Contributing, how we work, agent rules, and tests live under
Contribute. Architecture decisions live in `specs/decisions/`.

## Tests

Pytest. Product pages say what is true. Tests check the public API and
the constitution. Mock Pi RPC. Same change as the code. Checks before
commit are policy. Commands, suites, and what microvm needs are in
[tests](tests.md). See `CONTRIBUTING.md`.

## Contribute

Issues are the usual list of work. A PR without an issue is fine when
the change was asked directly. See `CONTRIBUTING.md` at the repo root.

## Agents

Rules: [agents.md](agents.md) (`AGENTS.md` at the repo root).
Steps: `.agents/skills/` (`plan`, `create-issue`, `work-issue`,
`review`, `release`). Those files stay in `.agents/skills/`.

If a behavior is missing from a product page, add it in the same
change or stop.

An issue is common. One checkout is one branch. Parallel work is best
effort when files do not overlap. Use another folder (`git worktree`)
or another session.
