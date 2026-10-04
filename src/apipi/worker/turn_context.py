"""Read the command context on the worker: files, skills, and MCP servers."""

import base64
import html
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apipi.common.dirs import store_root
from apipi.common.errors import store_error
from apipi.config import Settings
from apipi.env.setup import workspace_file_missing
from apipi.protocol.context import redact_url


def local_ref_path(settings: Settings, local_path: str) -> Path:
    """Resolve a context relative path inside the shared store root."""
    root = store_root(settings).resolve()
    candidate = (root / local_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise store_error(
            "context path escapes the store root",
            operation="get",
            key=local_path,
        )
    return candidate


async def fetch_ref_bytes(ref: Mapping[str, Any], settings: Settings) -> bytes:
    """Fetch one context file/skill/blob reference without DB access."""
    url = ref.get("url")
    if isinstance(url, str) and url:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
        except Exception as exc:
            raise store_error(
                f"cannot fetch turn context ref: {type(exc).__name__}",
                operation="get",
                key=redact_url(url),
            ) from exc
        if response.status_code != 200:
            raise store_error(
                f"cannot fetch turn context ref: HTTP {response.status_code}",
                operation="get",
                key=redact_url(url),
            )
        return response.content
    local_path = ref.get("local_path")
    if isinstance(local_path, str) and local_path:
        path = local_ref_path(settings, local_path)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise store_error(
                f"cannot read turn context ref: {exc}",
                operation="get",
                key=local_path,
            ) from exc
    raise store_error("turn context ref has no url or local_path", operation="get")


async def materialize_workspace_files(
    files: list[Any], settings: Settings | None, workspace: Path | None
) -> list[tuple[str, bytes]]:
    """Fetch the bytes of the referenced files that are missing in the workspace."""
    if not files or settings is None or workspace is None:
        return []
    materialized: list[tuple[str, bytes]] = []
    for ref in files:
        if not isinstance(ref, Mapping):
            continue
        path = ref.get("path")
        if not isinstance(path, str):
            continue
        if not workspace_file_missing(workspace, path):
            continue
        materialized.append((path, await fetch_ref_bytes(ref, settings)))
    return materialized


async def materialize_skill_zips(
    skills: list[Any], settings: Settings | None
) -> list[bytes]:
    """Fetch skill zip bytes for context references without DB access."""
    if not skills or settings is None:
        return []
    zips: list[bytes] = []
    for ref in skills:
        if not isinstance(ref, Mapping):
            continue
        zips.append(await fetch_ref_bytes(ref, settings))
    return zips


async def fetch_pi_session_bytes(
    pi_session: Mapping[str, Any] | None, settings: Settings | None
) -> bytes | None:
    """Fetch the cold-restore Pi session blob without DB access."""
    if not isinstance(pi_session, Mapping) or not pi_session.get("present"):
        return None
    if settings is None:
        raise store_error(
            "cannot restore the Pi session without settings", operation="get"
        )
    return await fetch_ref_bytes(pi_session, settings)


def _model_input(part: Any) -> str | None:
    if not isinstance(part, Mapping):
        return None
    if part.get("type") == "image":
        return "image"
    if part.get("type") == "file":
        return str(part.get("model_input") or "")
    return None


async def _fetch_part(part: Mapping[str, Any], settings: Settings) -> bytes:
    data = await fetch_ref_bytes(part, settings)
    size = part.get("size_bytes")
    if isinstance(size, int) and len(data) != size:
        raise store_error(
            f"input {part.get('type')} is {len(data)} bytes, expected {size}",
            operation="get",
            key=str(part.get("object_id") or ""),
        )
    return data


async def fetch_input_images(
    parts: list[Any], settings: Settings
) -> list[dict[str, str]]:
    """Fetch the `image` parts and image `file` parts of `turn.start` for Pi."""
    images: list[dict[str, str]] = []
    for part in parts:
        if _model_input(part) != "image":
            continue
        data = await _fetch_part(part, settings)
        images.append(
            {
                "type": "image",
                "data": base64.b64encode(data).decode(),
                "mimeType": str(part.get("mime_type") or "application/octet-stream"),
            }
        )
    return images


def file_block(filename: str, text: str) -> str:
    """The prompt text of one `input_file`: its text inside a `<file>` block."""
    body = text[:-1] if text.endswith("\n") else text
    return f'<file name="{html.escape(filename, quote=True)}">\n{body}\n</file>'


async def fetch_input_files(parts: list[Any], settings: Settings) -> list[str]:
    """Fetch the text `file` parts of `turn.start` as prompt blocks, in order.

    Raises `UnicodeDecodeError` when a file is not UTF-8 text.
    """
    blocks: list[str] = []
    for part in parts:
        if _model_input(part) != "text":
            continue
        data = await _fetch_part(part, settings)
        blocks.append(
            file_block(str(part.get("filename") or ""), data.decode("utf-8-sig"))
        )
    return blocks


def mcp_servers_from_context(context: Mapping[str, Any]) -> list[Any]:
    """Rebuild McpHttpServer objects from a parsed turn context."""
    from apipi.mcp.http import McpHttpServer

    raw = context.get("mcp")
    servers: list[Any] = []
    if not isinstance(raw, list):
        return servers
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = item.get("server_label")
        url = item.get("server_url")
        if not isinstance(label, str) or not isinstance(url, str):
            continue
        headers = item.get("headers")
        allowed = item.get("allowed_tools")
        servers.append(
            McpHttpServer(
                server_label=label,
                server_url=url,
                headers=dict(headers) if isinstance(headers, dict) else {},
                allowed_tools=(
                    tuple(i for i in allowed if isinstance(i, str))
                    if isinstance(allowed, list)
                    else ()
                ),
            )
        )
    return servers
