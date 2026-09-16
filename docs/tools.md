# Tools and skills

The base tools are Pi's four: read, write, edit, and bash. Those exist
when the session has a computer (`openai_hosted` or a connected
`self_hosted` runner). They do not exist when `environment.type` is
`none`. Everything else is attached per agent: function tools, MCP
servers, and skills.

Copy-paste configs live in `examples/` at the repo root (Tavily,
Playwright).

## Function tools

Caller-defined functions. The agent emits a `function_call` item. The
session goes `requires_action` with a `function_call` action. The
client posts `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` or `error`. That is the same idea as
OpenAI's Agents API. The gateway does not execute the function.

## MCP

HTTP MCP uses OpenAI's shape:

```json
{
  "type": "mcp",
  "server_label": "tavily",
  "server_url": "https://mcp.tavily.com/mcp",
  "headers": {
    "Authorization": "Bearer ${TAVILY_API_KEY}"
  }
}
```

Stdio MCP is an ApiPi extension for local servers that follow Pi (on
the host in `none` mode, inside the guest in `microvm` mode):

```json
{
  "type": "mcp",
  "server_label": "playwright",
  "command": "npx",
  "args": ["-y", "@playwright/mcp@latest", "--headless"]
}
```

An MCP tool must have `server_url` or `command`, not both. The gateway
connects HTTP servers when the session is created, then hands them to
Pi through a host credential broker. The guest does not receive MCP
bearers. Prefer a [vault](api.md#vaults) (`static_bearer` bound to
`mcp_server_url`, attach `vault_ids` on the session). Tool `headers`
with `${ENV}` still expand on the host. Stdio servers start next to
Pi: on the host in `none` mode, and inside the same guest in
`microvm` mode. Stdio credentials stay in environment variables, not
in git.

Search goes through MCP.

### Search — Tavily example

Tavily's hosted MCP is one search option. Set `TAVILY_API_KEY`. See
`examples/tavily.yaml`. You can swap that for Brave, Exa, or any other
server that speaks MCP.

### Browser — Playwright example

[Playwright MCP](https://playwright.dev/mcp/introduction) is one
browser option. `--headless` is the usual server flag. See
`examples/playwright.yaml`.

Sandbox size `L` on isolation `microvm` attaches that server for you
(system Chromium in the browser rootfs). You do not need to list it on
the agent. A caller-supplied Playwright MCP tool is not duplicated.
Turn auto-inject off with `APIPI_SANDBOX_AUTO_PLAYWRIGHT=false` if you
want L RAM and rootfs but manual MCP only.

The browser follows Pi (`none` or `microvm`). Inside a `microvm`,
Chromium can use its own sandbox in the guest.

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

There is no `/v1/skills` upload API. Skills are files on the computer.

## Per agent

MCP and function tools live on the saved agent (or the inline session
`agent`). Skills live on the computer, pointed at by
`capability_directories`. Changing tools later means updating the
saved agent; it does not rewrite history on existing sessions.
