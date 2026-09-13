---
name: create-issue
description: File one GitHub issue with Goal, Acceptance, and Specs. Use when filing a bug or request, or when plan is ready to file. Prefer an existing issue. Do not use to implement.
---

# Create an issue

Follow `CONTRIBUTING.md` Issues. If those rules disagree with this
skill, `CONTRIBUTING.md` wins.

Prefer an existing open issue (`gh issue list`). Do not duplicate. If
the user asked to skip an issue, stop.

Use `gh`. If it is missing or not logged in, stop. Tell the user to
install and log in to `gh`. Do not file another way unless they ask.

Write enough that a human and an agent can do the work without a
meeting. Do not squeeze the goal. Put known implementation details on
the issue.

Request:

```
gh issue create --title "<title>" --body "$(cat <<'EOF'
## Goal

<what should exist when this is done. As much as needed.>

## Acceptance

- <check>

## Specs

- <path>

## Details

<known implementation notes, if any>

## Files

- <file this change is likely to edit>

## Blocked by

<issue number or none>
EOF
)"
```

Bug: Observed, Expected, Specs, Reproduce instead of Goal / Acceptance.
Details, Files, and Blocked by are optional.

Do not list what this is not. Do not file `docs/roadmap.md` work unless
a product page has moved. If it is not on a product page, say so.

Return the issue URL. Do not start implementing.
