# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-09-14

First public release. Install from PyPI as `geki-apipi`. The import
package and CLI stay `apipi`.

### Added

- OpenAI Agents API compatible gateway (`apipi serve`) with Postgres as
  the source of truth.
- Pi harness over RPC. Run modes `none` (local/dev) and `microvm`
  (Firecracker). Production uses `microvm`.
- Agents, sessions, events, turns, items, artifacts, and export.
- Tenant-scoped auth via a callback. The gateway does not store keys.
- Optional S3-compatible artifact storage (`geki-apipi[s3]`).

### Changed

- Alembic history is a single baseline revision (`0001_initial`) that
  matches the current schema. Pre-0.1.0 databases have no upgrade path
  through the old revision chain. Recreate the database and run
  `apipi migrate`, or stamp `0001_initial` if the schema already
  matches.

[Unreleased]: https://github.com/GEKI-AI/apipi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/GEKI-AI/apipi/releases/tag/v0.1.0
