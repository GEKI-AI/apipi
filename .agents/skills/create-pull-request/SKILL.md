---
name: create-pull-request
description: Create a GitHub pull request for this repo using the GitHub CLI (gh) when available. Use when asked to open, create, or file a PR, pull request, or merge request.
---

# Create a pull request

Follow this repo's process, not a generic GitHub flow. Read before acting:

- `CONTRIBUTING.md`
- `AGENTS.md`
- `CONSTITUTION.md`
- `docs/process.md`

Do not invent process. If those files disagree with this skill, those files win.

## Preconditions

Stop and say what is missing if any of these fail:

1. An issue exists. A PR without an issue is the exception.
2. Current branch is `issue-<n>-<slug>`. One issue per branch and per PR.
3. You are not on `main`. Never force-push `main`.
4. The branch is on latest `origin/main` (see Trunk). Do not open a PR that will conflict.
5. Spec and code agree, or both change in this PR.
6. Checks are green locally. Do not open a red PR.

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

Do not commit red. Do not commit unless the user asked to commit or to open the PR.

## Trunk

This repo is trunk-based: short-lived branches off `main`, linear history, squash merge. Always update from latest `main` **before** creating the PR. Otherwise the PR is likely to conflict.

```
git fetch origin main
git rebase origin/main
```

If the rebase conflicts, stop. Do not create the PR. Report the conflicting files and wait.

If the rebase is clean and the branch was already pushed, update the remote with `--force-with-lease` on this issue branch only. Never force-push `main`.

After rebase, re-run the checks above. Then create the PR.

Do not merge `main` into the branch (`git merge origin/main`). Rebase. No merge commits.

## GitHub CLI

Check that `gh` is installed **before** creating the PR:

```
command -v gh
```

If `gh` is present, use it. Do not fall back to the API or a browser while `gh` works.

Then:

```
gh auth status
```

If `gh` is missing, not authenticated, or the command fails, stop. Tell the user to install and authenticate the GitHub CLI (`gh`), then retry. Do not open the PR another way unless they ask.

## Create with `gh`

1. Confirm the branch is pushed to `origin`.
2. Confirm no existing open PR for this branch (`gh pr view` or `gh pr list --head <branch>`). If one exists, return its URL.
3. Create against `main`:

```
gh pr create --base main --title "<title>" --body "$(cat <<'EOF'
Fixes #<n>

<what changed and why, short>

EOF
)"
```

Title: short, imperative, scoped to the issue. Body **starts with** `Fixes #<n>`.

4. Return the PR URL. Do not squash-merge, approve, or delete the branch. A human reviews, then squash-merges.

## Do not

- One PR for several issues
- Merge commits onto `main`
- A custom docs frontend, roadmap items that are not in a spec, or other `AGENTS.md` / constitution bans
- Tenant keys, secrets, or tokens in the PR
- `gh pr create --fill` without checking that the body starts with `Fixes #<n>`
