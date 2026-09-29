# Agent versions

The agent row is the live definition. A version is an explicit snapshot
of that row. Creating or editing an agent does not write a version, and
a session always uses the live row. An edit therefore changes the next
turn of a session that already exists.

Versions live under `/v1/apipi/agents/{id}/versions`. OpenAI's Agents API
has no agent version. If it adds one, these routes stay as aliases for at
least one minor release.

## What a snapshot stores

The snapshot holds every definition field: `name`, `model`,
`instructions`, `idle_ttl`, `metadata` (including `apipi.thinking` and
`apipi.system_prompt`), `tools`, `session_defaults`, and `reasoning`.
`service_tier` and `text` are not versioned because they are not
implemented.

Skills, files, vaults, and credentials are stored as ids only. A snapshot
does not keep skill zip contents, file bytes, vault contents, or
credential tokens. Re-uploading a skill, rotating a credential, or
deleting a file changes every snapshot that references that id.

`metadata["apipi.agent_version"]` has no meaning. It is stored like any
other metadata key.

## Create, restore, and delete

`POST /v1/apipi/agents/{id}/versions` snapshots the current live
definition. The body may set `name` and `comment`. It does not take a
definition and it does not activate anything. Numbers start at 1 for each
agent and are never reused, including after the newest snapshot is
deleted.

`GET` lists newest first. `{version}` is the id or the number.
`?include=definition` adds definitions to the list. One version always
includes its definition. The definition cannot be edited.

`POST /v1/apipi/agents/{id}/versions/{version}/restore` copies that
snapshot back into the agent row, in one transaction:

1. It checks that the model, skills, files, vaults, and credentials still
   exist. A missing reference is `400`. The agent row is unchanged and no
   snapshot is written.
2. It snapshots the current live definition with source `pre_restore` and
   a comment such as `before restore of v3`. Restoring that snapshot
   undoes the restore.
3. It copies the target definition into the agent row.
4. It applies retention.

The response is the updated agent plus `pre_restore_version`. Restoring
a definition that already matches the live row still writes the
pre-restore snapshot.

`DELETE` removes any snapshot. There is no "active" snapshot to protect.
The number is not reused.

`POST …/activate` is not a route.

## Retention

`APIPI_AGENT_VERSIONS_KEEP` defaults to 10 and must be an integer of 1 or
more. Anything else is a startup error. After a snapshot is written, the
oldest snapshots beyond that count are deleted. The automatic pre-restore
snapshot counts. Nothing else references versions, so the limit is exact.

On restore, the target definition is copied into the agent row before
pruning. If pruning then deletes that snapshot because it was the oldest,
the restore has still succeeded.

Deleting an agent deletes its snapshots.

## Export and templates

`GET /v1/apipi/agents/{id}/export?version=` exports that snapshot. The zip
manifest includes `source_version` with the id and number. Import does
not require that field. Omit `version` to export the live agent. That
export has no `source_version`.

Importing a bundle or instantiating a template creates an agent and no
snapshots. Snapshot the new agent explicitly if you want history.

A later session pin can use the same snapshot shape. Session turns read
the live definition through one function, so a pin can be added there
without a new table.
