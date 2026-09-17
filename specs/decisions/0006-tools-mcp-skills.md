# 0006. Tools, MCP, and skills

The gateway gives you sessions, events, and a computer. The agent loop
is Pi's four tools (read, write, edit, bash) plus whatever you attach.

Extend with:

- **Function tools** -- caller returns the result (`requires_action`)
- **MCP** -- HTTP (OpenAI shape) or stdio (local servers)
- **Skills** -- `SKILL.md` directories on the computer, same as the
  [Agent Skills](https://agentskills.io/home) standard and OpenAI's
  `environment.capability_directories`. Hosted packs upload to
  `/v1/skills` and attach with `environment.skills`
  `skill_reference`. Discovery is still from directories in the
  workspace after the pack is unpacked.

Search and browser are not built in. Examples: Tavily MCP, Playwright
MCP (`examples/`). Any other MCP server or skill pack is valid.

No first-party `web_search`. No browser engine in the gateway.
