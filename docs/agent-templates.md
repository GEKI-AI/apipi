# Agent templates

A template is a portable zip of one agent's configuration. Use it to
share an agent inside a tenant, or to ship a starter agent (a research
agent, a browser agent) without copying JSON by hand.

The bundle holds configuration only. It does not include sessions,
history, turns, events, artifacts, or usage. Creating an agent from a
template always creates a new agent in the caller's tenant. That agent
is an independent copy. A later change to the template, or deleting
it, does not change agents already created from it.

Templates are tenant-scoped. `visibility` is `tenant` in this version.
Another tenant's template is `404`.

## Routes

The routes, the template object, and the create-agent response are in
[API](api.md#templates). `GET /v1/agents/{agent_id}/export` builds the
same zip without storing a template.

## Bundle layout

The file is a zip, `application/zip`, usually named
`*.apipi-agent.zip`. The manifest is `agent.json` at the archive root.

```
agent.json
skills/<name>.zip
files/<path>
README.md
```

`README.md` is optional and ignored on import. Skill zips are the same
zip `GET /v1/skills/{id}/download` returns. Files are the bytes that
were inline or stored as `file_id` on the agent's session defaults.

`agent.json` uses schema version `1.0` and `kind` `apipi.agent`. It
includes `template` (name and description), `agent` (the same fields as
agent create, without `id` or timestamps), `image` (id and size only),
and `requires` (secret and credential names that the importer must
supply).

`image.built_version` may be present. Import does not require it and
does not compare it. The image is referenced by id and size only. The
blob is not in the zip.

## Secrets and credentials

Secret values are never written into the zip.

MCP header values become `{"$secret": "<NAME>"}`. If the stored value
was `${NAME}`, that name is kept. Otherwise the name is
`<SERVER_LABEL>_<HEADER>`.

Every `session_defaults.environment.env` value becomes
`{"$secret": "<KEY>"}`. There is no allowlist of safe env keys. Put
credentials in a vault and map them on create, instead of relying on
plain env values.

`credential_id` and `vault_ids` become `{"$credential": "<name>"}`.
Vault ids, credential ids, and tokens are not written. The names are
listed in `requires`.

On `POST /v1/templates/{id}/agents`, `secrets` maps those names to
values. Those values are stored where agent create already stores them:
header strings and env strings. `credentials` maps a name to a vault or
credential id in the caller's tenant. A credential id also fills
`vault_ids` with that credential's vault. A missing name is listed in
`missing` and the placeholder is left out. A mapping to an id this
tenant does not own is `400`.

## What fails and what warns

Upload rejects a bad zip, a missing `agent.json`, a `schema_version`
major newer than 1, an unsafe path, and an embedded skill that would
fail skill upload. Those failures store nothing.

An unknown image id, or a size below that image's minimum, is a warning
on upload and `400` when you create an agent. Other agent-write
validation (bad `idle_ttl`, hosted-only fields on the wrong type, too
many skills or files, disallowed chat tools) fails both upload and
create. A failed create stores no agent, skill, or file.

An unknown model does not fail create. It is listed in
`missing.models`. If the model host cannot be listed, create still
succeeds and the response includes a warning.

## Versioning

`schema_version` is `MAJOR.MINOR`. Export writes `1.0`. Upload accepts
any `1.x`. A newer major is `400` with code `bundle_version_unsupported`.
Unknown fields in the same major are ignored and listed in `warnings`.

## Limits and archive safety

The zip must fit `APIPI_MAX_FILE_BYTES`. The reader also rejects more
than 256 entries, a compression ratio above 100, absolute paths, `..`,
backslashes, drive letters, duplicate names, symlinks, and paths
outside `agent.json`, `README.md`, `skills/*.zip`, and `files/`.

## Provenance

An agent created from a template stores `metadata["apipi.template_id"]`
and `metadata["apipi.template_updated_at"]`. Those keys are not
interpreted. Export drops them, so a later bundle does not depend on
the template that produced the agent.
