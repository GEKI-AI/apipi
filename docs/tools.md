# Tools and skills

The base tools are Pi's four: read, write, edit, and bash. Those exist
when the session has a computer (`openai_hosted` or a connected
host computer). They do not exist when `environment.type` is
`none`. Everything else is attached per agent: function tools, MCP
servers, the built-in `web_search` tool, and skills.

`environment.type=none` sessions have no computer, so built-in tools
are always off and cannot be turned on (`apipi.builtin_tools=on` is
`400` with code `builtin_tools`). Only function tools, HTTP MCP with
`server_url`, and `web_search` are allowed; anything else is `400`
with code `tool_not_allowed`. These checks run on agent create and
update (when the agent's `session_defaults.environment.type` is `none`), on
session create (using the effective environment, tools, and metadata),
on session update (whenever metadata changes on a `type=none`
session), and on template import. An agent saved for hosted use with
tools or metadata that conflict with `type=none` cannot be used for a
`type=none` session. The worker forces built-ins off for `type=none`
even if a bad flag got through, and logs `pi.builtin_tools_forced_off`.
See [API](api.md) and [environments](environments.md#none).

Copy-paste configs live in `examples/` at the repo root (Tavily MCP).
The browser example is `examples/sessions/browser_screenshot.py`.

## Built-in tools

Pi's built-in tools are read, write, edit, and bash. They exist when
the session has a computer and `apipi.builtin_tools` is not `off`.
Set `apipi.builtin_tools` to `off` on the agent or session to run a
microVM session without shell and file tools. The session value
overrides the agent, and the default is `on`. With `off`, Pi starts
with `--no-builtin-tools` (when MCP or function tools are present) or
`--no-tools` (when neither is), and `--no-skills`, so skills are not
loaded. The guest still boots and still collects `outputs/`
artifacts. See [config](config.md#pi).

## Function tools

Caller-defined functions. The agent emits a `function_call` item. The
session goes `requires_action` with a `function_call` action. The
client posts `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` or `error`. That is the same idea as
OpenAI's Agents API. The gateway does not execute the function.

## MCP

MCP uses OpenAI's flat tool shape, served by Pi's built-in MCP client
over streamable HTTP. There is no stdio MCP.

```json
{
  "type": "mcp",
  "server_label": "docs",
  "server_url": "https://mcp.example.com/mcp",
  "headers": {"X-Api-Key": "public-value"},
  "allowed_tools": ["search"],
  "require_approval": "never",
  "server_description": "Product docs search"
}
```

`server_label` is required and must match `[A-Za-z0-9_-]`.
`server_url` is required and must be public HTTP(S): loopback, RFC 1918,
link-local, and other special-use addresses are rejected, including hostnames
that resolve to them. `headers` are sent as-is; values that contain `${...}`
are rejected because the gateway never expands environment variables into
caller-supplied headers. Store secrets in a [vault](api.md#vaults)
(`static_bearer` bound to `mcp_server_url`, attach `vault_ids` on the session)
and the host broker injects the bearer. `allowed_tools` is a list of tool names, or
`{"tool_names": [...]}`. Other filter forms return `not_implemented`.
`require_approval` accepts only `"never"` or an absent value; anything
else returns `not_implemented`. `server_description` is accepted and
shown to the model. `connector_id` and `authorization` return
`not_implemented`. `credential_id` and `required` work as before. A
nested `transport` object is an unknown field.

The gateway checks the tool shape, the headers, and the SSRF guard when
the session is created, then hands the servers to Pi through a host
credential broker. Pi's built-in MCP client is the only client: it connects
through the broker when the turn starts, so the gateway never probes the
server. A thin Pi extension calls `pi.registerMcpServer()` with the broker
URL, `direct` exposure, and the allowed tools. The guest does not receive
MCP bearers. Model facing tool names are `mcp__<server>__<tool>`, sanitised
and hashed when long. Pi writes `-` in the server label as `_`, so a
label `my-docs` gives `mcp__my_docs__<tool>`. Two labels that differ only in
`-` and `_` are the same name to Pi, so an agent or an inline session with
both is rejected with `400` and code `mcp_label_collision`. API output items
still carry the original `server_label` and tool name. A server that is down or rejects the call does not fail the
turn. The worker logs `pi.extension_error` with the server label, the phase,
and the error, and the turn continues without that server's tools.

Prefer a [vault](api.md#vaults) (`static_bearer` bound to
`mcp_server_url`, attach `vault_ids` on the session).

The SSRF guard runs when the session is created, when the broker starts,
and on every broker call: the
hostname is resolved and every address is checked, so DNS rebinding cannot
swap in a private address after the check. There are no redirects to follow.
Operators that run an MCP server on a private address (including local
development on `127.0.0.1`) list it in `APIPI_MCP_ALLOW_HOSTS` or
`[mcp].allow_hosts` (hostnames or CIDRs, for example
`mcp.internal, 10.0.0.0/8`). A blocked target fails session create; a target
that turns private later fails that call with a `502`, not the turn.

The MCP servers travel in the command context (see
[command context](workers.md#command-context)). The API resolves them
from the agent tools, the session vaults, and the SSRF guard on every
turn, so an HTTP MCP tool works on the first turn and on follow-ups,
even when a follow-up lands on another API replica. The worker hands
them to the broker. No MCP state is kept
in API process memory between turns.

A bash call with no `timeout` is capped at 120 seconds so a stuck
command cannot hold the turn until `APIPI_TURN_TIMEOUT`.

Search over MCP is one of two ways to search. The other is the
built-in [`web_search` tool](#web-search).

### Search over MCP: Tavily example

Tavily's hosted MCP is one search option. Create a vault credential with
auth type `static_bearer`, `mcp_server_url` `https://mcp.tavily.com/mcp`, and
the Tavily key as the token, then attach `vault_ids` on the session. See
`examples/tavily.yaml`. You can swap that for Brave, Exa, or any other
server that speaks MCP. This path keeps working. Use it when a tenant
must bring its own search key, or when you need a provider that ApiPi
does not support. For one operator key and central counting, use the
built-in tool below.

### Browser

The guest browser is not an MCP server. Image `browser` on isolation
`microvm` packs a built-in `browser` skill. The model uses bash and
`agent-browser`. Install the rootfs with
`apipi install --microvm --image browser`. The image is x86_64 only.
You do not list a Playwright server on the agent. The platform prompt
names the skill only when the resolved image is `browser`. It does
not claim a browser from sandbox size alone.

`/workspace/outputs` is not a working directory. Inspection
screenshots stay in `/workspace/.browser`. Copy a screenshot to
`outputs/` only when the user asked for that file. A small client is
`examples/sessions/browser_screenshot.py`.

## Web search

`web_search` is a built-in tool. Search runs on a provider that the
operator configures once on the API (Tavily or Staan, see
[config](config.md#search)). The agent does not name a provider, and no
search key reaches a worker or a guest. ApiPi does not crawl or index
the web. It calls the provider and passes the results to the model.

### Turn it on per agent

Add the OpenAI tool shape to the agent `tools` (or to the inline
session `agent`). Sessions and templates inherit it like any other
tool.

```json
{
  "type": "web_search",
  "filters": {"allowed_domains": ["docs.python.org", "peps.python.org"]}
}
```

| Field | What |
| --- | --- |
| `type` | Required. `web_search`. |
| `filters.allowed_domains` | Optional list of at most 10 domains. When set, results come only from those domains. Both providers take it as an include list. |

`search_context_size` and `user_location` return `not_implemented`.
The `web_search_preview` type returns `not_implemented`. Any other
unknown field returns `unknown_field`. See
[OpenAI compatibility](openai-compatibility.md#agent-fields-and-tools).

An agent with no `web_search` tool has no search tool in Pi. Creating
or updating an agent that includes `web_search` fails with `400` and
code `search_not_configured` when the operator has not configured a
search provider for the caller. ApiPi never drops the tool silently.

`web_search` works on `environment.type=none` sessions and on
`microvm` sessions. In a microVM the guest still reaches only the host
broker, as it does for the model and for MCP.

### What the model sees

The model gets a tool named `web_search` with these parameters.

| Parameter | What |
| --- | --- |
| `query` | Required. The search text. |
| `max_results` | Optional. How many results to return. The operator setting `APIPI_SEARCH_MAX_RESULTS` is a cap: a larger value is lowered to it. |

The result is a short numbered text list. Each entry has the title,
the URL, a snippet, and the publish date when the provider knows it.
Both providers produce the same list, so changing the provider does not
change what the model sees. The text is untrusted: it comes from pages
on the open web and can contain instructions aimed at the model. ApiPi
does not filter it, so write agent instructions that treat search
results as data.

A search that fails returns a tool error with a short message. The turn
does not fail, and the model may try again. A tool error happens when
the provider rejects the call, the provider call times out
(`APIPI_SEARCH_TIMEOUT`), the provider is unreachable, the query is
invalid, search is no longer allowed for the session, or the worker
loses its connection to the API while it waits. The API never
replays a search after a reconnect.

If the agent has the tool but search is not allowed when a turn starts
(for example the operator removed the provider after the agent was
saved), the turn still runs. The tool is not loaded for that turn, and
the API logs a `search.denied` warning with no query text.

### How it differs from search over MCP

| | `web_search` tool | Search over MCP |
| --- | --- | --- |
| Key | One operator key on the API | A vault credential per session |
| Where the key lives | API only | Host broker of the session |
| Provider | Operator's choice, swappable with no agent change | Fixed by the MCP server URL |
| Usage | Counted by the API per turn and day (`search_calls`, `search_units`) | Counted as an MCP call (`mcp_counts`) |
| Event log item | `web_search_call` | `mcp_call` |
| Tenant brings its own key | Not yet | Yes, with a vault |

Each search appears in the event log as a `web_search_call` item with
the query and a status of `in_progress`, `completed`, or `failed`. It
is not a `command_execution` or an `mcp_call` item. See
[API](api.md#turns-items-artifacts) and [usage](usage.md#search).

## Codemode

Codemode is an opt-in script tool. Set `apipi.codemode` to `off`
(default), `on`, or `only` on the agent or session. The session value
overrides the agent. Codemode needs built-in tools: `on` or `only`
together with `apipi.builtin_tools=off` is `400` with code
`builtin_tools`, including on `environment.type=none` sessions where
built-ins are always off.

The model writes JavaScript that runs in a QuickJS sandbox inside the
Pi process and can only call the other enabled tools, for example in
parallel with `Promise.allSettled`. It adds no new capabilities or
privileges; bash remains the boundary. In process run modes it runs on
the host inside Pi, like the rest of Pi. `models.classify()` and
`models.generateImages()` are unsupported and untested.

The `codemode` call itself is one `command_execution` item. Tool calls
made from scripts carry the parent call id and appear under the parent
item as `nested_calls`, not as top-level items. Nested MCP calls still
count in usage and in the turn log tallies. The outputs rules above
apply to files written by scripts: only explicitly requested artefacts
go to `/workspace/outputs`.

## Skills

[Agent Skills](https://agentskills.io/home) are a directory with
`SKILL.md` (name and description in front matter, then instructions).
Optional `scripts/`, `references/`, and `assets/` sit next to that
file.

On the OpenAI Agents API you put those directories on the computer and
list the parent paths in `environment.capability_directories` on
session create. The harness discovers `SKILL.md`, puts name and
description in context, and reads the rest when the skill is used.

ApiPi does the same. Pi already loads this format. On
`openai_hosted`, listed directories that sit outside the workspace are
copied into it when the session is created. In `microvm`, skill
directories from that workspace are packed into the guest and
`--skill` paths are rewritten to `/workspace`.

Also discovered, if present on the workspace:

- `.agents/skills/`
- `.pi/skills/`

Upload a skill zip with `POST /v1/skills` (multipart field `files`,
same 50 MiB cap as Files API). Attach it on session create with
`environment.skills`: `{ "type": "skill_reference", "skill_id": "…" }`.
ApiPi unpacks the zip under `.agents/skills/` so discovery works as
above. The zip must contain exactly one `SKILL.md`. Path traversal is
rejected. `capability_directories` still work for trees already on the
computer.

## Per agent

MCP, function, and `web_search` tools live on the saved agent (or the
inline session `agent`). Skills live on the computer, pointed at by
`capability_directories` or unpacked from `environment.skills`.
Changing tools later means updating the
saved agent; it does not rewrite history on existing sessions.
