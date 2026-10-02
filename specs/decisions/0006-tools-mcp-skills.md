# 0006. Tools, MCP, and skills

The gateway gives you sessions, events, and a computer. The agent loop
is Pi's four tools (read, write, edit, bash) plus whatever you attach.

Extend with:

- **Function tools** -- caller returns the result (`requires_action`)
- **MCP** -- HTTP (OpenAI shape)
- **Skills** -- `SKILL.md` directories on the computer, same as the
  [Agent Skills](https://agentskills.io/home) standard and OpenAI's
  `environment.capability_directories`. Hosted packs upload to
  `/v1/skills` and attach with `environment.skills`
  `skill_reference`. Discovery is still from directories in the
  workspace after the pack is unpacked.

Search has two paths. Any search MCP server works (example: Tavily
MCP in `examples/`), and a vault holds its key. The built-in
`web_search` tool is the second path. The guest browser is not MCP. It
is the built-in `browser` skill on the `browser` image, driven with
bash and `agent-browser`. This note supersedes the earlier Playwright
MCP example for the browser.

## `web_search`

An agent turns search on with `{"type": "web_search"}` in `tools`, the
OpenAI shape, with an optional `filters.allowed_domains`. The operator
configures one search provider on the API (`[search]` in config). The
first two providers are Tavily and Staan. A provider is one small
module behind a protocol that takes a query and returns normalized
results (title, URL, snippet, date when known) and the units the
provider charged. Changing the provider changes no agent, no tool, and
no event log.

The call is routed through the API. The Pi tool calls the session
broker, the worker forwards the request to the API over the worker
socket (`search.request` and `search.reply`), and the API calls the
provider. The request is synchronous and is not part of the durable
outbox. It carries no provider name, and the worker never holds a
provider key. The API checks on every request that the session belongs
to the tenant, that the turn is running, that the effective agent
tools include `web_search`, and that search is still allowed. It reads
`allowed_domains` from the agent definition, not from the worker.

This is an exception to Law 8 ("Thin on purpose"). A search MCP server
would be the usual answer, and it stays available. The built-in tool
exists for two reasons that an MCP server cannot meet:

- The key must stay in the API. An MCP server needs a credential on
  every session, and the credential reaches the worker or the guest
  broker. A provider key held by the API never reaches a worker or a
  guest.
- Usage must be counted where the call is made. The API makes the
  provider call, so the API counts it per tenant, turn, and day. It
  does not trust a worker tally.

ApiPi still does not crawl, index, or rank. It calls a provider and
passes the results on. That keeps "A search engine" in "What this is
not" true.

Every search decision goes through one resolver. It takes the tenant,
the user, and the organization and returns a target (provider, its
settings, the credential, and whether the key belongs to the operator
or the tenant) or nothing. Today it returns the global config for
everyone, or nothing when no provider is set. Allowing or denying
search per tenant or subject, and a tenant bringing its own provider
and key, change the resolver and nothing else: not the worker protocol,
not the Pi tool, and not the agent shape. Targets are not cached
between turns. Usage records the provider and the key source so an
operator can bill only operator-key searches later.

The browser rule is unchanged. There is no browser engine in the
gateway. The browser is a skill in the guest image.
