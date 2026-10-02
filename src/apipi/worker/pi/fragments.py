import logging
import re
from pathlib import Path

from apipi.config import ConfigError, Settings

log = logging.getLogger("apipi.worker.pi")

MAX_FRAGMENT_BYTES = 32 * 1024
MAX_PROMPT_BYTES = 120 * 1024
_VAR = re.compile(r"\$(\$\{|[A-Za-z_][A-Za-z0-9_]*|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_SHIPPED: dict[str, str] = {}
_PROMPTS = Path(__file__).resolve().parent / "prompts"

FILES: dict[str, str] = {
    "identity.none": "identity-none.txt",
    "identity.computer": "identity-computer.txt",
    "main.none": "none.txt",
    "main.hosted": "hosted.txt",
    "additional.none": "additional-none.txt",
    "additional.hosted": "additional-hosted.txt",
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


def fragment_source(settings: Settings, name: str) -> tuple[str | None, str | None]:
    override = settings.pi_prompts.get(name)
    if override is not None:
        return override, f"pi.prompts.{name}"
    if name.startswith("main.") and settings.platform_prompt is not None:
        return settings.platform_prompt, "APIPI_PLATFORM_PROMPT"
    if name.startswith("additional."):
        extra = settings.platform_prompt_additional
        if extra:
            return extra, "APIPI_PLATFORM_PROMPT_ADDITIONAL"
    return None, None


def fragment_body(settings: Settings, name: str) -> str:
    override, source = fragment_source(settings, name)
    if override is not None and source:
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
    if override is not None and source:
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
    return env_type == "openai_hosted"
