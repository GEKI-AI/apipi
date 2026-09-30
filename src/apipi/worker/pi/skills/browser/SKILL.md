---
name: browser
description: Drive the guest browser with agent-browser. Use for opening pages, reading them, clicking, filling forms, and screenshots the user asked for.
---

# Browser

The guest already has `agent-browser` and Chrome. Do not install a browser, Chrome, Playwright, or agent-browser. Do not run `agent-browser install` or `agent-browser upgrade`. The binary is read-only.

Use `snapshot -i` (interactive elements only). Act with `@eN` refs from that snapshot. Take a fresh snapshot after navigation or any page change. Prefer `batch` to chain steps. Add `--json` when you need to parse the output.

```bash
agent-browser open https://example.com
agent-browser snapshot -i
agent-browser click @e1
agent-browser snapshot -i
```

```bash
agent-browser batch \
  "open https://example.com" \
  "snapshot -i" \
  "fill @e1 hello" \
  "press Enter"
```

Refs look like `@e1 [button] "Continue"`. Reuse a ref only until the page changes.

## Working files and outputs

`/workspace/outputs` is not a working directory. Only a file the user explicitly asked for goes there.

Inspection screenshots, snapshots, logs, HAR, and traces stay in `/workspace/.browser` or `/tmp`. The image already points screenshots at `/workspace/.browser/screenshots` and downloads at `/workspace/.browser/downloads`. When the user asks for a screenshot, copy that one file to `/workspace/outputs`.

```bash
agent-browser screenshot /workspace/.browser/screenshots/page.png
agent-browser screenshot --full /workspace/.browser/screenshots/full.png
agent-browser pdf /workspace/.browser/page.pdf
```

`screenshot --if-changed` skips an unchanged image. `snapshot -i --delta` prints a full tree once, then only the changes.

## Common actions

```bash
agent-browser fill @e2 "hello"
agent-browser type @e2 " world"
agent-browser press Enter
agent-browser select @e4 "option-value"
agent-browser wait --text "Success"
agent-browser wait --url "**/dashboard"
agent-browser get title
agent-browser get url
agent-browser get text @e1
agent-browser find role button click --name "Submit"
```

The daemon starts on the first call and stays up for this session. Cookies persist until the sandbox stops. A new session is clean. Do not set a profile, state file, or restore path.

A project file `./agent-browser.json` is merged over the image defaults if you create one. Leave it unset unless you need a local override.

CDP, the stream server, and the dashboard listen on localhost only. Do not expose them. WebMCP is off. Do not set `AGENT_BROWSER_PROVIDER`.
