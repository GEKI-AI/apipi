# How we work

Specs describe the product as it is. Code follows. If they disagree, fix
the spec or the code in the same change.

Work that is not in this version lives in [roadmap.md](roadmap.md), not
on the spec pages.

| Kind | Where |
| --- | --- |
| Rules | `CONSTITUTION.md` |
| Standing orders | `AGENTS.md` |
| Specs | `docs/*.md` |
| Decisions | `docs/decisions/*.md` |
| Tests | `tests/` |
| Later | `docs/roadmap.md` |

Name, license, MkDocs, git, and tooling are baseline (`README.md`,
`LICENSE`, `mkdocs.yml`, `requirements-docs.txt`, `CONTRIBUTING.md`,
`pyproject.toml`). They do not get ADRs.

## Change a decision

Edit the ADR and the specs in the same change. Do not keep a superseded
copy. Do not add an ADR for something that is just a fact of the repo.

Clarifications (event names, copy) do not need an ADR.

## Docs site

Separate from the apipi package. `requirements-docs.txt`, not
`pyproject.toml`.

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

Markdown in git is the source. Do not add a custom docs app.

## Tests

Pytest. Specs are the oracle. Tests assert the public API and the
constitution, not Pi internals. Mock Pi RPC. Same change as the code.
Checks before commit are policy, not git hooks. See `CONTRIBUTING.md`.

## Contribute

Issues are the work queue. See `CONTRIBUTING.md` at the repo root.

## Agents

Standing orders: [agents.md](agents.md) (`AGENTS.md` at the repo root).
Do not add features that are not in a spec. Do not implement the roadmap
unless the spec has moved.
