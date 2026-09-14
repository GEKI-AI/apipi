---
name: release
description: Tag origin/main so GitHub builds, opens a Release, and publishes to PyPI. Use when asked to release, tag, or publish. Do not bump the version. Do not draft a GitHub Release by hand.
---

# Release

Follow `CONTRIBUTING.md` Releasing. If it disagrees with this skill, it
wins.

Do not bump `__version__`. Do not edit `CHANGELOG.md`. Those land in a
normal PR first. Do not create the GitHub Release in the UI. Do not tag
a feature branch.

If you have uncommitted work, stop. Do not `git reset --hard`.

Use `gh`. If it is missing or not logged in, stop.

## Tag `origin/main` only

```
git fetch origin main
```

Read `__version__` from `src/apipi/__init__.py` on `origin/main`, not
this checkout if it differs.

```
git show origin/main:src/apipi/__init__.py
git show origin/main:CHANGELOG.md
```

Stop unless all of these hold:

- The user asked to release, tag, or publish.
- `__version__` is `X.Y.Z` (three numeric parts).
- `CHANGELOG.md` on `origin/main` has a `## [X.Y.Z]` section.
- `origin/main` has `.github/workflows/publish.yml`.
- Tag `vX.Y.Z` does not already exist (`git ls-remote --tags origin`).
- If the user named a version, it matches `__version__`.

```
git tag -a "vX.Y.Z" origin/main -m "geki-apipi X.Y.Z"
git push origin "vX.Y.Z"
```

Never force-push a tag. Never force-push `main`.

## Wait

The Publish workflow builds, uploads to PyPI (`geki-apipi`), then
opens the GitHub Release and attaches the wheel and sdist.

```
gh run watch --exit-status $(gh run list --workflow=publish.yml --limit 1 --json databaseId --jq '.[0].databaseId')
gh release view "vX.Y.Z" --json url --jq .url
```

If the run fails, stop. Do not delete or move the tag.

Default: return the GitHub Release URL and
`https://pypi.org/project/geki-apipi/X.Y.Z/`.
