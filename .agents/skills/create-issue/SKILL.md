---
name: create-issue
description: Create a GitHub issue for this repo using the GitHub CLI (gh) when available. Use when starting a session, filing a bug or request, or when work has no issue yet. Prefer an existing issue if one already covers the work.
---

# Create an issue

Default process: start from an issue, do the work, end with a PR. That is the usual path, **not law**. If the user explicitly asks to skip the issue or to open a PR without one, do that.

At session start:

1. If the user pointed at an issue, use it.
2. Else look for an existing open issue that already is this work (`gh issue list`). Reuse it. Do not duplicate.
3. Else, most of the time, create an issue, then work from it.

Read before acting:

- `CONTRIBUTING.md`
- `AGENTS.md`
- `CONSTITUTION.md`
- `docs/process.md`

Do not invent process. If those files disagree with this skill, those files win.

## What to file

Write so an agent can implement without asking questions.

| Kind | Include |
| --- | --- |
| Request | Goal, acceptance checks, spec paths, non-goals |
| Bug | Observed, expected, spec path, how to reproduce |

Do not file work that lives on `docs/roadmap.md` unless the spec has moved. If it is not in a spec, say so; do not treat the issue as a license to build it anyway.

## GitHub CLI

Check that `gh` is installed **before** creating the issue:

```
command -v gh
```

If `gh` is present, use it. Do not fall back to the API or a browser while `gh` works.

Then:

```
gh auth status
```

If `gh` is missing, not authenticated, or the command fails, stop. Tell the user to install and authenticate the GitHub CLI (`gh`), then retry. Do not open the issue another way unless they ask.

## Create with `gh`

```
gh issue create --title "<title>" --body "$(cat <<'EOF'
## Goal

<one or two sentences>

## Acceptance

- <check>
- <check>

## Spec paths

- <path>

## Non-goals

- <what this is not>
EOF
)"
```

For a bug, use Observed / Expected / Spec path / How to reproduce instead of Goal / Acceptance / Non-goals.

Title: short, specific. Return the issue URL. Then work on that issue (branch `issue-<n>-<slug>` from latest `main`) unless the user says otherwise.
