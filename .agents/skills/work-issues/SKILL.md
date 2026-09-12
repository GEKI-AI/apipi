---
name: work-issues
description: Work through one or more GitHub issues in this repo, one at a time. Use when asked to keep working on issues, run the issue queue, implement open issues, or land a set of slices. Sequential implement → checks → PR → CI → review → next.
---

# Work a set of issues

This is the loop for a queue of GitHub issues. Follow this repo's
process, not a generic GitHub flow. Read before acting:

- `CONTRIBUTING.md`
- `AGENTS.md`
- `CONSTITUTION.md`
- `docs/process.md`

Do not invent process. If those files disagree with this skill, those
files win.

Default process: start from an issue, do the work, end with a PR. One
issue per branch and per pull request. Sequential. Do not implement
two issues on the same branch or in parallel on overlapping files.

Filing an issue: `.agents/skills/create-issue/SKILL.md`.
Opening a PR: `.agents/skills/create-pull-request/SKILL.md`.

## Queue

1. If the user named issues or an order, use that.
2. Else `gh issue list --state open`. Reuse existing issues. Do not
   duplicate.
3. Specs before the code that needs them. If code contradicts a spec,
   that issue is next.
4. Do not build `docs/roadmap.md` unless the spec has moved.

After each merge, re-read `CONSTITUTION.md`, `docs/index.md`, the spec
for the next surface, and the current code. Do not rely on memory from
the previous slice.

## Per issue

### 1. Trunk

```
git fetch origin main
git checkout main
git reset --hard origin/main
```

Branch `issue-<n>-<slug>` from that `main`. Never commit on `main`.

### 2. Specs

Before code, read the issue body and its spec paths. If it is not in a
spec, update the spec in this change or stop. Acceptance checks in the
issue are the scope. Anything not in the goal is out of scope.

### 3. Implement

Only this issue. Spec and code change together when they disagree.
Tenant-scope every query. Do not store tenant keys. No Pi types on
HTTP. Unknown OpenAI fields fail clearly.

uv only. No comments unless the issue or the user asked.

### 4. Local checks

Do not commit red.

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

Then commit on the issue branch. Write a short imperative message.

### 5. Pull request

Follow `.agents/skills/create-pull-request/SKILL.md`. Rebase onto
latest `origin/main` first. Body starts with `Fixes #<n>`.

Wait until GitHub `Check`, `Tests`, and `Docs` are green
(`gh pr checks --watch`). Do not merge red.

### 6. Review

Launch a review sub-agent with: the PR diff vs `main`, the issue body,
the spec paths, `CONSTITUTION.md`, `AGENTS.md`. It must answer:

- Only this issue's goal?
- Spec and code agree?
- Tenant-scoped? No stored secrets? No Pi types on HTTP?
- Tests cover the acceptance checks?
- Any constitution or `AGENTS.md` ban?

Apply must-fixes, re-run local checks, push, wait for CI. Skip nits
that are not spec violations.

### 7. Merge

Do not merge unless the user explicitly asked you to merge after
review. Default: stop and return the PR URL. A human reviews, then
squash-merges.

When the user did ask:

```
gh pr merge --squash --delete-branch
```

If branch protection blocks it, stop and report. Never force-push
`main`. Never merge commits onto `main`.

### 8. Next

Confirm the issue is closed. `git checkout main && git pull origin main`.
Start the next issue at step 1. Do not batch leftover work onto the
merged branch.

## Do not

- One PR for several issues
- Parallel branches that both edit `runtime.py` or `api/sessions.py`
- Merge commits onto `main`
- Roadmap items that are not in a spec
- Tenant keys, secrets, or tokens in the PR
- Silent fallback between run modes
- Continue past a red check or a rebase conflict
