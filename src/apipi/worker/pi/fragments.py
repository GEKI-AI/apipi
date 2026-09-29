import logging
import os
import re
from pathlib import Path

from apipi.config import ConfigError, Settings

log = logging.getLogger("apipi.worker.pi")

MAX_FRAGMENT_BYTES = 32 * 1024
MAX_PROMPT_BYTES = 120 * 1024
_VAR = re.compile(r"\$(\$\{|[A-Za-z_][A-Za-z0-9_]*|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_CACHE: dict[str, tuple[int, str]] = {}

IDENTITY = (
    "You are an expert coding assistant operating inside ${platform_name}, "
    "a coding agent harness."
)
MAIN_NONE = (
    "This session runs on ${platform_name}. There is no computer and no file or "
    "shell tools. Do not invent APIs or tools that this session does not "
    "provide."
)
MAIN_HOSTED = (
    "This session runs on ${platform_name}. The working directory is ${workspace}. "
    "An idle or TTL stop (${idle_ttl}) deletes the workspace, possibly "
    "mid-conversation. The next message starts a fresh sandbox. The "
    "conversation history persists, but files outside outputs/ do not. "
    "Files provided by the user are under inputs/. Write files the user "
    "should receive under outputs/. Those files are published when a turn "
    "completes and stay downloadable after the sandbox is gone. Do not "
    "invent APIs or tools that this session does not provide."
)
MAIN_SELF = (
    "This session runs on ${platform_name}. The working directory is the runner's "
    "files. Write files the user should receive under outputs/. Those files "
    "are published when a turn completes and stay downloadable. Other files "
    "are scratch and are not published. Do not invent APIs or tools that "
    "this session does not provide."
)
SIZE = "Sandbox size is ${size} (${mem_mib} MiB)."
NETWORK_ENABLED = "This sandbox has network access."
NETWORK_RESTRICTED = "This sandbox has restricted network access."
MCP_TOOL = (
    "Use ${tool_name} for ${server_label} MCP (${tool}). "
    "Do not reimplement it with bash."
)
PLAYWRIGHT = (
    "${chromium}Drive the browser only through these MCP tools. "
    "Save screenshots under outputs/. "
    "Do not npm install playwright or download browsers."
)
BASH_INSTALL = (
    "Do not install Playwright or browser binaries. Use the Playwright MCP tools."
)

DEFAULTS: dict[str, str] = {
    "identity": IDENTITY,
    "main.none": MAIN_NONE,
    "main.hosted": MAIN_HOSTED,
    "main.self_hosted": MAIN_SELF,
    "additional.none": "",
    "additional.hosted": "",
    "additional.self_hosted": "",
    "size": SIZE,
    "network": "",
    "capability": "",
    "mcp_tool": MCP_TOOL,
    "playwright": PLAYWRIGHT,
    "bash_install_block": BASH_INSTALL,
}

VARIABLES = frozenset(
    {
        "platform_name",
        "env_type",
        "workspace",
        "size",
        "mem_mib",
        "vcpus",
        "image",
        "network",
        "idle_ttl",
        "date",
        "has_browser",
        "tool_name",
        "server_label",
        "tool",
        "chromium",
    }
)

_ENV = {
    "identity": ("APIPI_PLATFORM_IDENTITY", "APIPI_PLATFORM_IDENTITY_FILE"),
    "main.none": ("APIPI_PLATFORM_PROMPT_NONE", "APIPI_PLATFORM_PROMPT_NONE_FILE"),
    "main.hosted": (
        "APIPI_PLATFORM_PROMPT_HOSTED",
        "APIPI_PLATFORM_PROMPT_HOSTED_FILE",
    ),
    "main.self_hosted": (
        "APIPI_PLATFORM_PROMPT_SELF_HOSTED",
        "APIPI_PLATFORM_PROMPT_SELF_HOSTED_FILE",
    ),
    "additional.none": (
        "APIPI_PLATFORM_PROMPT_NONE_ADDITIONAL",
        "APIPI_PLATFORM_PROMPT_NONE_ADDITIONAL_FILE",
    ),
    "additional.hosted": (
        "APIPI_PLATFORM_PROMPT_HOSTED_ADDITIONAL",
        "APIPI_PLATFORM_PROMPT_HOSTED_ADDITIONAL_FILE",
    ),
    "additional.self_hosted": (
        "APIPI_PLATFORM_PROMPT_SELF_HOSTED_ADDITIONAL",
        "APIPI_PLATFORM_PROMPT_SELF_HOSTED_ADDITIONAL_FILE",
    ),
    "size": ("APIPI_PLATFORM_SIZE", "APIPI_PLATFORM_SIZE_FILE"),
    "network": ("APIPI_PLATFORM_NETWORK", "APIPI_PLATFORM_NETWORK_FILE"),
    "capability": ("APIPI_PLATFORM_CAPABILITY", "APIPI_PLATFORM_CAPABILITY_FILE"),
    "mcp_tool": ("APIPI_PLATFORM_MCP_TOOL", "APIPI_PLATFORM_MCP_TOOL_FILE"),
    "playwright": ("APIPI_PLATFORM_PLAYWRIGHT", "APIPI_PLATFORM_PLAYWRIGHT_FILE"),
    "bash_install_block": (
        "APIPI_PLATFORM_BASH_INSTALL",
        "APIPI_PLATFORM_BASH_INSTALL_FILE",
    ),
}


def render_template(
    text: str,
    values: dict[str, str],
    *,
    fragment: str,
    source: str,
    strict: bool,
) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        if token == "${":
            return "${"
        name = match.group(2) or token
        if name not in VARIABLES:
            if strict:
                raise ConfigError(
                    f"unknown variable {name} in fragment {fragment} ({source})"
                )
            log.warning(
                "fragment render failed",
                extra={"fragment": fragment, "variable": name},
            )
            return match.group(0)
        return values.get(name, "")

    return _VAR.sub(repl, text)


def _read_file(path: str, *, fragment: str, strict: bool) -> str | None:
    file = Path(path)
    try:
        stat = file.stat()
    except OSError:
        if strict:
            raise ConfigError(f"fragment {fragment} file is missing: {path}") from None
        log.warning("fragment file missing", extra={"fragment": fragment, "path": path})
        return None
    cached = _CACHE.get(path)
    if cached and cached[0] == stat.st_mtime_ns:
        return cached[1]
    try:
        text = file.read_text()
    except OSError:
        if strict:
            raise ConfigError(
                f"fragment {fragment} file is unreadable: {path}"
            ) from None
        log.warning(
            "fragment file unreadable", extra={"fragment": fragment, "path": path}
        )
        return None
    if len(text.encode()) > MAX_FRAGMENT_BYTES:
        if strict:
            raise ConfigError(f"fragment {fragment} file is too large")
        log.warning("fragment file too large", extra={"fragment": fragment})
        return None
    _CACHE[path] = (stat.st_mtime_ns, text)
    return text


def fragment_source(settings: Settings, name: str) -> tuple[str | None, str | None]:
    text_env, file_env = _ENV[name]
    text = os.environ.get(text_env)
    file = os.environ.get(file_env)
    if text is not None and file:
        raise ConfigError(
            f"set {text_env} or {file_env}, not both, for fragment {name}"
        )
    if (
        name.startswith("main.")
        and settings.platform_prompt is not None
        and text is None
        and not file
    ):
        return settings.platform_prompt, "APIPI_PLATFORM_PROMPT"
    if name.startswith("additional.") and not text and not file:
        extra = settings.platform_prompt_additional
        if extra:
            return extra, "APIPI_PLATFORM_PROMPT_ADDITIONAL"
    if text is not None:
        return text, text_env
    if file:
        return file, file_env
    return None, None


def fragment_text(
    settings: Settings,
    name: str,
    values: dict[str, str],
    *,
    strict: bool,
) -> str:
    override, source = fragment_source(settings, name)
    if source and source.endswith("_FILE") and override:
        loaded = _read_file(override, fragment=name, strict=strict)
        if loaded is None:
            cached = _CACHE.get(override)
            body = cached[1] if cached else DEFAULTS[name]
        else:
            body = loaded
        source_name = source
    elif override is not None and source and not source.endswith("_FILE"):
        body = override
        source_name = source
    else:
        body = DEFAULTS[name]
        source_name = "built-in"
    rendered = render_template(
        body, values, fragment=name, source=source_name, strict=strict
    )
    if len(rendered.encode()) > MAX_FRAGMENT_BYTES:
        raise ConfigError(f"fragment {name} is too large")
    return rendered


def validate_fragments(settings: Settings) -> None:
    values = {name: "" for name in VARIABLES}
    values["platform_name"] = settings.platform_name
    for name in DEFAULTS:
        fragment_text(settings, name, values, strict=True)


def capability_block(values: dict[str, str]) -> str:
    network = values.get("network") or "disabled"
    return (
        f"Image is {values.get('image') or 'default'}. "
        f"Size is {values.get('size') or 'S'} "
        f"({values.get('mem_mib') or '0'} MiB, {values.get('vcpus') or '1'} vCPUs). "
        f"Network access is {network}. "
        "Do not assume a browser is available. "
        "Use Playwright MCP tools only if they are registered."
    )


def cap_prompt(text: str) -> str:
    raw = text.encode()
    if len(raw) <= MAX_PROMPT_BYTES:
        return text
    raise ConfigError("composed platform prompt is too large")
