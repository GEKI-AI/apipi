import re
from typing import Any

from apipi.config import ConfigError, Settings
from apipi.gateway.errors import ApiError

SANDBOX_SIZES = frozenset({"S", "M", "L"})
SANDBOX_SIZE_KEY = "apipi.sandbox_size"
SANDBOX_IMAGE_KEY = "apipi.sandbox_image"
SANDBOX_SIZE_HELP = "sandbox_size must be S, M, or L"
SANDBOX_IMAGE_HELP = "sandbox_image must match ^[a-z0-9][a-z0-9-]{0,31}$"
IMAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SIZE_RANK = {"S": 0, "M": 1, "L": 2}
PLAYWRIGHT_LABEL = "playwright"
PLAYWRIGHT_CHROMIUM = "/usr/bin/chromium-browser"


def parse_sandbox_size(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_SIZES:
        raise ApiError(
            "invalid_request",
            SANDBOX_SIZE_HELP,
            code="invalid_request",
        )
    return value


def sandbox_size_of(environment: dict[str, Any] | None) -> str:
    if not environment:
        return "S"
    raw = environment.get("sandbox_size")
    if raw is None:
        return "S"
    parsed = parse_sandbox_size(raw)
    return parsed if parsed is not None else "S"


def image_for_size(size: str) -> str:
    if size == "L":
        return "browser"
    return "default"


def parse_sandbox_image(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or IMAGE_ID.fullmatch(value) is None:
        raise ApiError(
            "invalid_request",
            SANDBOX_IMAGE_HELP,
            code="invalid_request",
        )
    return value


def image_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or SANDBOX_IMAGE_KEY not in metadata:
        return None
    return parse_sandbox_image(metadata.get(SANDBOX_IMAGE_KEY))


def resolve_sandbox_image(
    *,
    environment_image: str | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    size: str,
    default: str,
) -> str:
    if environment_image is not None:
        parsed = parse_sandbox_image(environment_image)
        if parsed is not None:
            return parsed
    session_image = image_from_metadata(session_metadata)
    if session_image is not None:
        return session_image
    agent_image = image_from_metadata(agent_metadata)
    if agent_image is not None:
        return agent_image
    if size == "L":
        return "browser"
    parsed = parse_sandbox_image(default)
    return parsed if parsed is not None else "default"


def sandbox_image_of(environment: dict[str, Any] | None) -> str | None:
    if not environment:
        return None
    raw = environment.get("sandbox_image")
    if not isinstance(raw, str):
        return None
    return parse_sandbox_image(raw)


def min_size_for_image(image_id: str) -> str | None:
    from apipi.worker.pi.install import images_root, read_image_env

    try:
        env = read_image_env(images_root() / image_id / "image.env")
    except (OSError, ConfigError):
        env = {}
    raw = env.get("MIN_SIZE")
    if raw in SANDBOX_SIZES:
        return raw
    if image_id == "browser":
        return "M"
    if image_id == "default":
        return "S"
    return None


def require_image_size(image_id: str, size: str) -> None:
    floor = min_size_for_image(image_id)
    if floor is None:
        return
    if _SIZE_RANK[size] < _SIZE_RANK[floor]:
        raise ApiError(
            "invalid_request",
            f"sandbox_image {image_id} needs sandbox_size {floor} or larger",
            code="invalid_request",
        )


def size_for_mem(settings: Settings, mem_mib: int) -> str:
    if mem_mib >= settings.sandbox_l_mem_mib:
        return "L"
    if mem_mib >= settings.sandbox_m_mem_mib:
        return "M"
    return "S"


def size_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    if SANDBOX_SIZE_KEY not in metadata:
        return None
    return parse_sandbox_size(metadata.get(SANDBOX_SIZE_KEY))


def resolve_sandbox_size(
    *,
    environment_size: str | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    default: str,
) -> str:
    if environment_size is not None:
        parsed = parse_sandbox_size(environment_size)
        if parsed is not None:
            return parsed
    session_size = size_from_metadata(session_metadata)
    if session_size is not None:
        return session_size
    agent_size = size_from_metadata(agent_metadata)
    if agent_size is not None:
        return agent_size
    parsed = parse_sandbox_size(default)
    return parsed if parsed is not None else "S"


def mem_mib_for_size(settings: Settings, size: str | None) -> int:
    return settings.sandbox_mem_mib(size if size is not None else "S")


def playwright_tool(settings: Settings) -> dict[str, Any]:
    return {
        "type": "mcp",
        "server_label": PLAYWRIGHT_LABEL,
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": [
                "-y",
                settings.sandbox_playwright_mcp,
                "--headless",
                "--isolated",
                "--no-sandbox",
                "--output-dir=/workspace/outputs",
                f"--executable-path={PLAYWRIGHT_CHROMIUM}",
            ],
        },
    }


def _tool_blob(tool: dict[str, Any]) -> str:
    transport = tool.get("transport")
    if isinstance(transport, dict):
        command = transport.get("command")
        raw_args = transport.get("args")
    else:
        command = None
        raw_args = None
    args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
    parts = [str(command)] if command is not None else []
    parts.extend(args)
    return " ".join(parts)


def has_playwright(tools: list[Any] | None) -> bool:
    if not tools:
        return False
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "mcp":
            continue
        label = tool.get("server_label")
        if isinstance(label, str) and label.lower() == PLAYWRIGHT_LABEL:
            return True
        if "playwright/mcp" in _tool_blob(tool):
            return True
    return False


def playwright_attached(stdio: list[Any] | None) -> bool:
    if not stdio:
        return False
    for server in stdio:
        if isinstance(server, dict):
            label = server.get("server_label")
            args = server.get("args")
        else:
            label = getattr(server, "server_label", None)
            args = getattr(server, "args", None)
        if isinstance(label, str) and label.lower() == PLAYWRIGHT_LABEL:
            return True
        blob = " ".join(str(item) for item in args) if isinstance(args, list) else ""
        if "playwright/mcp" in blob:
            return True
    return False


def should_inject_playwright(
    settings: Settings, size: str, image: str | None = None
) -> bool:
    selected = image if image is not None else image_for_size(size)
    return (
        selected == "browser"
        and settings.sandbox_auto_playwright
        and settings.run_mode == "microvm"
    )


def merge_playwright(
    tools: list[Any] | None,
    *,
    size: str,
    settings: Settings,
    image: str | None = None,
) -> list[Any]:
    out = list(tools) if tools else []
    if not should_inject_playwright(settings, size, image):
        return out
    if has_playwright(out):
        return out
    return [*out, playwright_tool(settings)]


def require_known_image(settings: Settings, image_id: str) -> None:
    if settings.sandbox_images is not None:
        known = image_id in settings.sandbox_images
    elif image_id in {"default", "browser"}:
        known = True
    else:
        from apipi.worker.pi.image_pull import available_images

        known = any(item.id == image_id for item in available_images(settings))
    if not known:
        raise ApiError(
            "invalid_request",
            f"unknown sandbox_image {image_id}",
            code="invalid_request",
        )


def require_image_rootfs(settings: Settings, image_id: str) -> None:
    if settings.api_only or settings.run_mode != "microvm":
        return
    from apipi.worker.pi.microvm import microvm_images

    try:
        microvm_images(settings, image=image_id)
    except ConfigError as exc:
        raise ApiError("invalid_request", str(exc), code="invalid_request") from exc


def require_size_rootfs(settings: Settings, size: str) -> None:
    require_image_rootfs(settings, image_for_size(size))
