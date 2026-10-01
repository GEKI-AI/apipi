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
_SHIPPED: dict[str, str] = {}
_PROMPTS = Path(__file__).resolve().parent / "prompts"

FILES: dict[str, str] = {
    "identity.none": "identity-none.txt",
    "identity.computer": "identity-computer.txt",
    "main.none": "none.txt",
    "main.hosted": "hosted.txt",
    "additional.none": "additional-none.txt",
    "additional.hosted": "additional-hosted.txt",
    "size": "size.txt",
    "network": "network.txt",
    "network.enabled": "network-enabled.txt",
    "network.restricted": "network-restricted.txt",
    "capability": "capability.txt",
    "browser": "browser.txt",
    "mcp_tool": "mcp-tool.txt",
}

GUIDELINE_FILES: dict[str, str] = {
    "mcp_tool": "mcp-tool.txt",
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
    }
)

_ENV = {
    "identity.none": (
        "APIPI_PLATFORM_IDENTITY_NONE",
        "APIPI_PLATFORM_IDENTITY_NONE_FILE",
    ),
    "identity.computer": (
        "APIPI_PLATFORM_IDENTITY_COMPUTER",
        "APIPI_PLATFORM_IDENTITY_COMPUTER_FILE",
    ),
    "main.none": ("APIPI_PLATFORM_PROMPT_NONE", "APIPI_PLATFORM_PROMPT_NONE_FILE"),
    "main.hosted": (
        "APIPI_PLATFORM_PROMPT_HOSTED",
        "APIPI_PLATFORM_PROMPT_HOSTED_FILE",
    ),
    "additional.none": (
        "APIPI_PLATFORM_PROMPT_NONE_ADDITIONAL",
        "APIPI_PLATFORM_PROMPT_NONE_ADDITIONAL_FILE",
    ),
    "additional.hosted": (
        "APIPI_PLATFORM_PROMPT_HOSTED_ADDITIONAL",
        "APIPI_PLATFORM_PROMPT_HOSTED_ADDITIONAL_FILE",
    ),
    "size": ("APIPI_PLATFORM_SIZE", "APIPI_PLATFORM_SIZE_FILE"),
    "network": ("APIPI_PLATFORM_NETWORK", "APIPI_PLATFORM_NETWORK_FILE"),
    "network.enabled": (
        "APIPI_PLATFORM_NETWORK_ENABLED",
        "APIPI_PLATFORM_NETWORK_ENABLED_FILE",
    ),
    "network.restricted": (
        "APIPI_PLATFORM_NETWORK_RESTRICTED",
        "APIPI_PLATFORM_NETWORK_RESTRICTED_FILE",
    ),
    "capability": ("APIPI_PLATFORM_CAPABILITY", "APIPI_PLATFORM_CAPABILITY_FILE"),
    "browser": ("APIPI_PLATFORM_BROWSER", "APIPI_PLATFORM_BROWSER_FILE"),
    "mcp_tool": ("APIPI_PLATFORM_MCP_TOOL", "APIPI_PLATFORM_MCP_TOOL_FILE"),
}

_IDENTITY_ENV = ("APIPI_PLATFORM_IDENTITY", "APIPI_PLATFORM_IDENTITY_FILE")


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


def load_shipped() -> None:
    if _SHIPPED:
        return
    loaded: dict[str, str] = {}
    for name, filename in FILES.items():
        path = _PROMPTS / filename
        try:
            text = path.read_text()
        except OSError:
            raise ConfigError(f"prompt template missing: {filename}") from None
        if len(text.encode()) > MAX_FRAGMENT_BYTES:
            raise ConfigError(f"prompt template {filename} is too large")
        loaded[name] = text.strip("\n")
    _SHIPPED.update(loaded)


def shipped_text(name: str) -> str:
    load_shipped()
    try:
        return _SHIPPED[name]
    except KeyError:
        raise ConfigError(f"prompt template missing: {name}") from None


def render_shipped(name: str, values: dict[str, str]) -> str:
    return render_template(
        shipped_text(name),
        values,
        fragment=name,
        source="built-in",
        strict=False,
    )


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


def _pair(
    text_env: str, file_env: str, *, fragment: str
) -> tuple[str | None, str | None]:
    text = os.environ.get(text_env)
    file = os.environ.get(file_env)
    if text is not None and file:
        raise ConfigError(
            f"set {text_env} or {file_env}, not both, for fragment {fragment}"
        )
    if text is not None:
        return text, text_env
    if file:
        return file, file_env
    return None, None


def fragment_source(settings: Settings, name: str) -> tuple[str | None, str | None]:
    text_env, file_env = _ENV[name]
    text, source = _pair(text_env, file_env, fragment=name)
    if text is not None or source:
        return text, source
    if name.startswith("main.") and settings.platform_prompt is not None:
        return settings.platform_prompt, "APIPI_PLATFORM_PROMPT"
    if name.startswith("additional."):
        extra = settings.platform_prompt_additional
        if extra:
            return extra, "APIPI_PLATFORM_PROMPT_ADDITIONAL"
    if name.startswith("identity."):
        shared, shared_source = _pair(*_IDENTITY_ENV, fragment=name)
        if shared is not None or shared_source:
            return shared, shared_source
    return None, None


def fragment_body(settings: Settings, name: str) -> str:
    override, source = fragment_source(settings, name)
    if source and source.endswith("_FILE") and override:
        loaded = _read_file(override, fragment=name, strict=False)
        if loaded is None:
            cached = _CACHE.get(override)
            return cached[1].strip("\n") if cached else shipped_text(name)
        return loaded.strip("\n")
    if override is not None and source and not source.endswith("_FILE"):
        return override
    return shipped_text(name)


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
            body = cached[1] if cached else shipped_text(name)
        else:
            body = loaded
        source_name = source
    elif override is not None and source and not source.endswith("_FILE"):
        body = override
        source_name = source
    else:
        body = shipped_text(name)
        source_name = "built-in"
    rendered = render_template(
        body, values, fragment=name, source=source_name, strict=strict
    )
    if len(rendered.encode()) > MAX_FRAGMENT_BYTES:
        raise ConfigError(f"fragment {name} is too large")
    return rendered.strip("\n")


def validate_fragments(settings: Settings) -> None:
    load_shipped()
    values = {name: "" for name in VARIABLES}
    values["platform_name"] = settings.platform_name
    for name in FILES:
        fragment_text(settings, name, values, strict=True)


def capability_block(settings: Settings, values: dict[str, str]) -> str:
    filled = {
        **values,
        "image": values.get("image") or "default",
        "size": values.get("size") or "S",
        "mem_mib": values.get("mem_mib") or "0",
        "vcpus": values.get("vcpus") or "1",
        "network": values.get("network") or "disabled",
        "date": "${date}",
    }
    if filled["image"] == "browser":
        filled["has_browser"] = fragment_text(settings, "browser", filled, strict=False)
    else:
        filled["has_browser"] = "Do not assume a browser is available."
    return fragment_text(settings, "capability", filled, strict=False)


def cap_prompt(text: str) -> str:
    raw = text.encode()
    if len(raw) <= MAX_PROMPT_BYTES:
        return text
    raise ConfigError("composed platform prompt is too large")


def computer_identity(env_type: str | None) -> bool:
    return env_type in {"openai_hosted", "hosted"}
