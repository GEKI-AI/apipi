# 0013. Agent templates

A template is a tenant-scoped zip of one agent's configuration, stored
in the existing artifact store under the `templates` namespace.

The bundle is configuration only. It never includes sessions, history,
or secret values. Creating an agent from a template always creates a
new agent. There is no live link. Deleting the template does not change
agents already created from it.

`schema_version` is `MAJOR.MINOR`. Upload rejects a newer major.
Unknown fields in the same major are warnings. Visibility is `tenant`
in this version.

Secrets and credentials are placeholders in the zip. The caller
supplies values when creating an agent. Those values go into the same
fields agent create already stores. Env values are always placeholders.
There is no safe-env allowlist.

Image references are an id and a size. The image blob is not in the
bundle.
