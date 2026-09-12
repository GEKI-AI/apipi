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

Request:

```
gh issue create --title "<title>" --body "$(cat <<'EOF'
## Goal

<one or two sentences>

## Acceptance

- <check>

## Specs

- <path>

## Files

- <file this change is likely to edit>

## Blocked by

<issue number or none>
EOF
)"
```

Bug: Observed, Expected, Specs, Reproduce instead of Goal / Acceptance.
Files and Blocked by are optional.

Do not list what this is not. Do not file `docs/roadmap.md` work unless
the spec has moved. If it is not in a spec, say so.

Return the issue URL. Do not start implementing.
