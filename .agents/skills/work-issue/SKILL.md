---
name: work-issue
description: Implement one issue or one asked change and open a PR. Use when implementing or opening a pull request. Do not use to plan a set of issues.
---

# Work one change

Follow `CONTRIBUTING.md`. If it disagrees with this skill, it wins.

An issue is common, not required. If the user skipped it, implement the
asked change on a short branch name. If they named an issue, do only
that issue.

One checkout is one branch. If you have uncommitted work, stop. Do not
`git reset --hard`. Do not implement two changes in this folder.

If another open PR already edits the same files, stop.

## Branch

```
git fetch origin main
```

Branch `issue-<n>-<slug>` from `origin/main`, or a short name if there
is no issue. Never commit on `main`. For a second change at the same
time, use another folder (see `.agents/skills/plan/SKILL.md`).

Before code: `CONSTITUTION.md` and the spec for this part. What the
issue said to check is the scope. If it is not in a spec, update the
spec in this change or stop.

## Checks and PR

Do not commit if checks fail.

```
./scripts/check
```

If docs changed, `./scripts/check --docs`.

Commit when the user asked to implement, commit, or open a PR. Short
message that says what you did.

Rebase onto `origin/main` before the PR. If that conflicts, stop. Do
not merge `main` into the branch. After a clean rebase you may update
this branch with `--force-with-lease`. Never force-push `main`.

Use `gh`. If it is missing or not logged in, stop.

```
gh pr create --base main --title "<title>" --body "$(cat <<'EOF'
Fixes #<n>

<what changed and why, short>
EOF
)"
```

Omit `Fixes #<n>` when there is no issue. If a PR for this branch
already exists, return its URL.

Wait for GitHub `Check`, `Tests`, and `Docs` (`gh pr checks --watch`).
Do not merge if checks fail.

Default: return the PR URL. Merge only when the user asks to merge
(`gh pr merge --squash --delete-branch`). Never force-push `main`.
