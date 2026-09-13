# Specs

Small tree for architecture decisions. The product contract is the
MkDocs pages under `docs/`. Agents read those pages for the part they
are changing.

| Path | What |
| --- | --- |
| `docs/` | Product and operator docs. HTTP and run-mode contract. |
| `specs/decisions/` | ADRs. Read when architecture changes. |
| `CONSTITUTION.md` | Laws |
| `AGENTS.md` | Agent rules |
| `CONTRIBUTING.md` | Git, issues, checks |

Do not duplicate `docs/` here. Edit an ADR in place when a decision
changes. Add an ADR only when architecture actually changes.
