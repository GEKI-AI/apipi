# Roadmap

Not in the first version. Product pages under `docs/` describe what we
build now. This file is the rest.

Do not put "later" sections on the product pages. Do not implement this
file unless a product page for that work has moved.

## Docker sandbox

A container as the session computer. Same session API as the local
directory. Can run on the API host or on another machine.

## Workers

Workers register with the API and advertise `pi`, `sandbox`, or both.

- Split Pi and computers onto different pools
- Run workers on other machines
- Assign a session to a worker

Until then the API process runs Pi (`none` / `microvm`) plus
the local directory or a `self_hosted` runner.

## Remote browser

Point Playwright MCP at a remote browser (`--cdp-endpoint`), or run it
in a Docker sandbox.

## Another harness

Pi is what we ship. A second adapter behind the same API, if needed.

## Docs

Later documentation work, not this version:

- Guides for common setups (a hosted model URL, MCP, a `self_hosted`
  runner)
- OpenAPI and mkdocstrings for the FastAPI surface, on the same MkDocs
  site

Run-mode production docs (packages, systemd, Docker, storage) are in
[run modes](run-modes.md). Host sizing, scale-out, and drain are in
[production](production.md).

Do not add a custom docs frontend. Do not put MkDocs in the apipi
package.
