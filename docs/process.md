# How we work

Product pages under `docs/` describe the gateway as it is. Code
follows. If they disagree, fix the page or the code in the same
change.

Work that is not in this version lives on the [roadmap](roadmap.md),
not on the product pages. Do not implement the roadmap unless a
product page has moved.

| Kind | Where |
| --- | --- |
| Laws | `CONSTITUTION.md` |
| Agent rules | `AGENTS.md` |
| Product | `docs/` |
| Decisions | `specs/decisions/` |
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
Opinion changes belong in the product page that is wrong, not in a new
history file.

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

See `CONTRIBUTING.md`.

## Change a decision

Edit the decision file and the product pages in the same change. Do
not keep an old copy. When a decision changes, edit the ADR in
`specs/decisions/`. Law 9 in the constitution already says this.

Do not add a new ADR for copy, naming, or process nits. Add a new ADR
only when architecture actually changes. Do not add a decision file for
something that is just a fact of the repo. ADRs are not on the MkDocs
site.

## Docs site

The MkDocs site is separate from the apipi package. Dependencies live in
`requirements-docs.txt`, not `pyproject.toml`. Markdown in `docs/` is
the source. Do not add a custom docs app.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

Home, Get started, and Reference are for people who want to run the
product. Contributing, how we work, agent rules, and the roadmap live
under Contribute. Architecture decisions live in `specs/decisions/`.

## Tests

Pytest. Product pages say what is true. Tests check the public API and
the constitution, not Pi internals. Mock Pi RPC. Same change as the
code. Checks before commit are policy, not git hooks. Commands, suites,
and what jail and microvm need are in [tests](tests.md). See
`CONTRIBUTING.md`.

## Contribute

Issues are the usual list of work. A PR without an issue is fine when
the change was asked directly. See `CONTRIBUTING.md` at the repo root.

## Agents

Rules: [agents.md](agents.md) (`AGENTS.md` at the repo root).
Steps: `.agents/skills/` (`plan`, `create-issue`, `work-issue`,
`review`). Do not copy those into `AGENTS.md`.

Do not add features that are not on a product page. Do not implement
the roadmap unless a product page has moved.

An issue is common, not required. One checkout is one branch.
Parallel work is best effort when files do not overlap. Use another
folder (`git worktree`) or another session.
