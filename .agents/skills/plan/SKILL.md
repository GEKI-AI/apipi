---
name: plan
description: Split a goal into small GitHub issues. Use when planning new work or creating a set of issues. Do not use when implementing an existing issue.
---

# Plan

Read `CONSTITUTION.md` and the spec for the part you are changing. Do
not write product code. Do not build `docs/roadmap.md` unless the spec
has moved.

Propose issues first. File them only when the user agrees, using
`.agents/skills/create-issue/SKILL.md`.

## Split

Each issue is one goal, few files, and can merge on its own.

- Prefer a new module or function over two issues editing the same files.
- If two later issues would edit the same file, land shared code first.
- If the spec must change, file that issue first.
- Anything not in the goal is out of scope. Do not list what this is
  not unless someone would think it was included.

## Work at the same time

One checkout is one branch. Two changes at the same time need different
**Files** and another folder or session.

```
git fetch origin main
git worktree add -b issue-<n>-<slug> ../apipi-issue-<n> origin/main
```

Two issues may run at the same time only if Files do not overlap and
neither lists the other under **Blocked by**.

## Each proposed issue

| Field | What |
| --- | --- |
| Title | Short and clear |
| Goal | One or two sentences |
| Acceptance | What you can check when it is done |
| Specs | Paths under `docs/` |
| Files | Files this change is likely to edit |
| Blocked by | Issue that must land first, if any |

Reuse an existing open issue when it already is this work. Do not
duplicate.
