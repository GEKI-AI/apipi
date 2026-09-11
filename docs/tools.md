# Tools and skills

The base is Pi's four tools: read, write, edit, bash (when there is a
computer). Everything else is attached per agent.

Copy-paste configs live in `examples/` at the repo root.

## Function tools

Caller-defined functions. The session goes `requires_action`. The client
posts `agent.session.input.tool_result`. Same idea as OpenAI.

## MCP

HTTP (OpenAI shape):

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

Stdio (not in OpenAI's API; we accept it for local servers):

```json
{
  "type": "mcp",
  "server_label": "playwright",
  "command": "npx",
  "args": ["-y", "@playwright/mcp@latest", "--headless"]
}
```

The gateway starts or connects these for the session and hands them to
Pi. Credentials stay in env or secret store, not in git.

`web_search` as a first-party OpenAI tool is not implemented. Use MCP.

### Search -- Tavily example

Tavily's hosted MCP. Set `TAVILY_API_KEY`. See `examples/tavily.yaml`.
Swap for Brave, Exa, or anything else that speaks MCP.

### Browser -- Playwright example

[Playwright MCP](https://playwright.dev/mcp/introduction). `--headless`
on a server. See `examples/playwright.yaml`.

The browser follows Pi (`host` / `jail` / `microvm`). Inside `jail`,
Chromium needs `--no-sandbox`. Inside `microvm`, Chromium can use its
own sandbox.

Do not put Chromium in the gateway.

## Skills

[Agent Skills](https://agentskills.io/home): a directory with `SKILL.md`
(name + description in front matter, then instructions). Optional
`scripts/`, `references/`, `assets/`.

OpenAI Agents API: put those directories on the computer and list the
parent paths in `environment.capability_directories` on session create.
The harness discovers `SKILL.md`, puts name and description in context,
and reads the rest when the skill is used.

We do the same. Pi already loads this format.

Also discovered, if present on the workspace:

- `.agents/skills/`
- `.pi/skills/`

No `/v1/skills` upload API. Skills are files on the computer.

## Per agent

MCP and function tools live on the saved agent (or inline session
`agent`). Skills live on the computer, pointed at by
`capability_directories`.
