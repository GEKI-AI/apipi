from apipi.config import Settings

DEFAULT_PLATFORM_PROMPT = (
    "This session runs on ApiPi.\n"
    "\n"
    "When a computer is present, the working directory is /workspace on a "
    "hosted sandbox, or the runner's files for self_hosted. Write durable "
    "deliverables under outputs/ only. Those files are published when a "
    "turn completes and stay downloadable after the sandbox expires. Other "
    "files are scratch and are deleted with the workspace.\n"
    "\n"
    "When environment type is none, there is no computer and no file or "
    "shell tools.\n"
    "\n"
    "Do not invent APIs or tools that this session does not provide."
)

BROWSER_HINT = (
    "Chromium is already installed at /usr/bin/chromium-browser. "
    "Drive it only through the Playwright MCP tools (names start with "
    "mcp_playwright_). Save screenshots under outputs/. Do not npm install "
    "playwright, do not download browsers, and do not call chromium from bash."
)


def sandbox_size_hint(size: str | None) -> str:
    if size == "L":
        return (
            "Sandbox size is L (about 2 GiB, browser rootfs). "
            "System Chromium is already present. Do not install Playwright, "
            "Chromium, or browser packages with bash or npm."
        )
    if size in {"S", "M"}:
        return (
            f"Sandbox size is {size}. There is no browser in this sandbox. "
            "Do not install Playwright or Chromium."
        )
    if size:
        return f"Sandbox size is {size}."
    return ""


def compose_instructions(
    settings: Settings | None,
    agent_instructions: str | None,
    *,
    browser: bool = False,
    sandbox_size: str | None = None,
) -> str | None:
    if settings is None or settings.platform_prompt is None:
        main = DEFAULT_PLATFORM_PROMPT
    else:
        main = settings.platform_prompt
    extra = "" if settings is None else settings.platform_prompt_additional
    size = sandbox_size_hint(sandbox_size)
    hint = BROWSER_HINT if browser else ""
    agent = agent_instructions or ""
    parts = [part for part in (main, extra, size, hint, agent) if part]
    return "\n\n".join(parts) or None
