# Agent versions

An agent definition is saved as an immutable version. `GET /v1/agents/{id}`
returns the active version, in the same shape as before, plus
`active_version`. Editing an agent writes a new version and activates it.
A session keeps the version it was created with. A later edit does not
change that session.

Versions live under `/v1/apipi/agents/{id}/versions`. OpenAI's Agents API
has no agent version. If it adds one, these routes stay as aliases for at
least one minor release.

## What a version stores

The snapshot holds every definition field: `name`, `model`,
`instructions`, `idle_ttl`, `metadata`, `tools`, `session_defaults`, and
`reasoning`. `service_tier` and `text` are not versioned because they are
not implemented.

Skills, files, vaults, and credentials are stored as ids. The snapshot
does not copy secret values or file bytes. Rotating a vault credential
applies to every version that references it.

`status` is `ready` for every version in this release. Creation and
activation are separate service calls. A later draft or approval flow can
add statuses and an approver on the activation row without a new table.

## Create, activate, and delete

`POST /v1/agents` creates version 1 and activates it. `POST /v1/agents/{id}`
writes a new version only when the definition changes, and activates it.
A no-op update does not create a version.

`POST /v1/apipi/agents/{id}/versions` snapshots the active definition, or
takes a full definition. It does not activate unless `activate` is true.

`POST /v1/apipi/agents/{id}/versions/{version}/activate` makes that version
active. `{version}` is the id or the number. Activating an older number is
how you roll back. No new version is written. Activation checks that the
model, skills, files, vaults, and credentials still exist. A missing
reference is `400` and the active version does not change. Each activation
records who did it, when, and which version was active before.

`DELETE` is allowed only when the version is not active and no session or
turn still uses it. Numbers are never reused.
`APIPI_AGENT_VERSIONS_KEEP` prunes the oldest unreferenced inactive
versions beyond that count. Unset means keep them all.

Deleting an agent deletes its versions. Session version columns become
null with the agent id. A later turn on that session has no saved
definition, the same as today.

## Sessions and turns

A session with `agent_id` stores `agent_version`. The default is the
active version. `metadata["apipi.agent_version"]` pins a number or id.
Stock clients can send that key. `"active"` is not implemented.

The runtime reads that pinned definition, not the live agent row.
`POST /v1/agents/sessions` and `/v1/apipi/chat/sessions` both use this
rule. Inline agents have `agent_version` null.

A turn stores the same version. Session and turn responses, usage events,
and lifecycle events include `agent_version`.

To move a session, update `metadata["apipi.agent_version"]` while the
session is not `in_progress`. The next turn uses the new version. A move
during `in_progress` is `400`.

## Export

`GET /v1/apipi/agents/{id}/export?version=` exports that version. Omit
`version` to export the active one. The zip manifest may include
`source_version`. Import does not require it. Instantiating a template
creates a new agent at version 1 with source `template`. Version history
is not copied.
