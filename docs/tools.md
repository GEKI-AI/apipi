# Tools and skills

The base tools are Pi's four: read, write, edit, and bash. Those exist
when the session has a computer (`openai_hosted` or a connected
`self_hosted` runner). They do not exist when `environment.type` is
`none`. Everything else is attached per agent: function tools, MCP
servers, and skills.

`/v1/chat` sessions have no computer, so bash and file tools stay off.
Chat allows function tools and HTTP MCP only. Stdio MCP, Playwright
auto-inject, and workspace skills are rejected with code `chat_tool`.
See [Chat](api.md#chat).

Copy-paste configs live in `examples/` at the repo root (Tavily,
Playwright).

## Function tools

Caller-defined functions. The agent emits a `function_call` item. The
session goes `requires_action` with a `function_call` action. The
client posts `agent.session.input.tool_result` with `turn_id`,
`call_id`, `success`, and `output` or `error`. That is the same idea as
OpenAI's Agents API. The gateway does not execute the function.

## MCP

HTTP and stdio MCP use OpenAI's nested `transport` shape:

```json
{
  "type": "mcp",
  "server_label": "tavily",
  "transport": {
    "type": "http",
    "server_url": "https://mcp.tavily.com/mcp"
  },
  "headers": {
    "Authorization": "Bearer ${TAVILY_API_KEY}"
  }
}
```

```json
{
  "type": "mcp",
  "server_label": "playwright",
  "transport": {
    "type": "stdio",
    "command": "npx",
    "args": ["-y", "@playwright/mcp@latest", "--headless"]
  }
}
```

Top-level `server_url`, `command`, or `args` on the tool are unknown
fields. Unknown `transport.type` values return `400`. The gateway
connects HTTP servers when the session is created, then hands them to
Pi through a host credential broker. The guest does not receive MCP
bearers. Prefer a [vault](api.md#vaults) (`static_bearer` bound to
`mcp_server_url`, attach `vault_ids` on the session). Tool `headers`
with `${ENV}` still expand on the host. Stdio servers start next to
Pi: on the host in `none` mode, and inside the same guest in
`microvm` mode. Pi does not speak MCP by itself. ApiPi loads a Pi
extension that starts each stdio server, lists its tools, and
registers them on Pi as `mcp_<server_label>_<tool>`. If a listed
stdio server cannot start, Pi exits instead of running without those
tools. Optional `transport.cwd` is the process working
directory. Stdio credentials stay in environment variables, not in
git. Bash that tries to install Playwright or browser binaries is
blocked. A bash call with no `timeout` is capped at 120 seconds so a
stuck install cannot hold the turn until `APIPI_TURN_TIMEOUT`.

Search goes through MCP.

### Search — Tavily example

Tavily's hosted MCP is one search option. Set `TAVILY_API_KEY`. See
`examples/tavily.yaml`. You can swap that for Brave, Exa, or any other
server that speaks MCP.

### Browser — Playwright example

[Playwright MCP](https://playwright.dev/mcp/introduction) is one
browser option. `--headless` is the usual server flag. See
`examples/playwright.yaml`.

Image `browser` on isolation `microvm` attaches the vendored
Playwright MCP server for you (system Chromium in that rootfs). The
command is `node` and a fixed `cli.js` path, not `npx`. Install the
rootfs with `apipi install --microvm --image browser`. You do not
need to list the server on the agent. A caller-supplied Playwright
MCP tool is not duplicated. Turn auto-inject off with
`APIPI_SANDBOX_AUTO_PLAYWRIGHT=false` if you want that image and its
RAM but manual MCP only. The platform prompt names the sandbox size.
It does not name Playwright MCP tools. Those names are registered
only after the guest attach succeeds. Save screenshots under
`outputs/`.

The browser follows Pi (`none` or `microvm`). Inside a `microvm`,
Chromium can use its own sandbox in the guest. A small client is
`examples/sessions/browser_screenshot.py`.

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

MCP and function tools live on the saved agent (or the inline session
`agent`). Skills live on the computer, pointed at by
`capability_directories` or unpacked from `environment.skills`.
Changing tools later means updating the
saved agent; it does not rewrite history on existing sessions.
