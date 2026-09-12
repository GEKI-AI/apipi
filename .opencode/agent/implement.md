---
name: implement
description: Implement one GitHub issue in this repo. Use when working the issue queue, landing a slice, or the work-issues skill says to implement. Specs, code, tests, local checks. Do not open or merge the PR.
mode: subagent
---

You implement one GitHub issue in apipi. The parent named the issue
number. Do only that issue.

Read before code:

- `CONSTITUTION.md`
- `docs/index.md`
- `docs/decisions/`
- The issue body (`gh issue view N`)
- Spec paths on the issue
- `AGENTS.md`
- `CONTRIBUTING.md`

If it is not in a spec, update the spec in this change or stop. Do not
build `docs/roadmap.md`. Acceptance checks are the scope.

Branch is already `issue-<n>-<slug>` from latest `main`, or create it
if you are on `main`. Never commit on `main`.

Implement:

- Only this issue. Spec and code together when they disagree.
- Tenant-scope every query. Do not store tenant keys.
- No Pi types on HTTP. Unknown OpenAI fields fail clearly.
- Pin Pi when touching the adapter.
- uv only. No comments unless asked.

Local checks. Do not commit red:

```
uv run ruff format src tests
uv run ruff check src tests
uv run ty check src tests
uv run pytest
```

If docs changed:

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs build --strict
```

Commit on the issue branch with a short imperative message.

Return to the parent: what changed, how to verify, leftover risk.
Do not `gh pr create`. Do not merge. Do not start the next issue.
