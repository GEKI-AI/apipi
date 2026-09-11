# How we work

Specs describe the product as it is. Code follows. If they disagree, fix
the spec or the code in the same change.

Work that is not in this version lives in [roadmap.md](roadmap.md), not
on the spec pages.

| Kind | Where |
| --- | --- |
| Rules | `CONSTITUTION.md` |
| Specs | `docs/*.md` |
| Decisions | `docs/decisions/*.md` |
| Later | `docs/roadmap.md` |

Name, license, and MkDocs are baseline (`README.md`, `LICENSE`,
`mkdocs.yml`). They do not get ADRs.

## Change a decision

Edit the ADR and the specs in the same change. Do not keep a superseded
copy. Do not add an ADR for something that is just a fact of the repo.

Clarifications (event names, copy) do not need an ADR.

## Docs site

```
pip install -r requirements-docs.txt
mkdocs serve
```

Markdown in git is the source. Do not add a custom docs app.

## Agents

Standing orders: [agents.md](agents.md). Do not add features that are
not in a spec. Do not implement the roadmap unless the spec has moved.
