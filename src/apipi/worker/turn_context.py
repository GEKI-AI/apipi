"""Read the command context on the worker: files, skills, and MCP servers."""

import base64
import html
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from apipi.common.dirs import store_root
from apipi.common.errors import ObjectStoreError, store_error
from apipi.config import Settings
from apipi.env.setup import workspace_file_missing
from apipi.protocol.context import ContextEnvCredential, redact_url


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


async def fetch_ref_bytes(
    ref: Mapping[str, Any], settings: Settings, *, limit: int | None = None
) -> bytes:
    """Fetch one context file/skill/blob reference without DB access.

    The read stops after `limit` bytes, or after the `size_bytes` of the
    reference when `limit` is None, and a larger object fails.
    """
    if limit is None:
        size = ref.get("size_bytes")
        limit = size if isinstance(size, int) else None
    url = ref.get("url")
    if isinstance(url, str) and url:
        return await _fetch_url(url, limit)
    local_path = ref.get("local_path")
    if isinstance(local_path, str) and local_path:
        path = local_ref_path(settings, local_path)
        try:
            with path.open("rb") as handle:
                data = handle.read() if limit is None else handle.read(limit + 1)
        except OSError as exc:
            raise store_error(
                f"cannot read turn context ref: {exc}",
                operation="get",
                key=local_path,
            ) from exc
        _check_limit(data, limit, local_path)
        return data
    raise store_error("turn context ref has no url or local_path", operation="get")


def _check_limit(data: bytes | bytearray, limit: int | None, key: str) -> None:
    if limit is not None and len(data) > limit:
        raise store_error(
            f"turn context ref is larger than {limit} bytes",
            operation="get",
            key=key,
        )


async def _fetch_url(url: str, limit: int | None) -> bytes:
    import httpx

    try:
        async with (
            httpx.AsyncClient(timeout=30.0) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise store_error(
                    f"cannot fetch turn context ref: HTTP {response.status_code}",
                    operation="get",
                    key=redact_url(url),
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                _check_limit(data, limit, redact_url(url))
            return bytes(data)
    except ObjectStoreError:
        raise
    except Exception as exc:
        raise store_error(
            f"cannot fetch turn context ref: {type(exc).__name__}",
            operation="get",
            key=redact_url(url),
        ) from exc


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
        zips.append(
            await fetch_ref_bytes(ref, settings, limit=int(settings.max_file_bytes))
        )
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


def size_text(size: int) -> str:
    """A short size for the prompt: `512 B`, `240 KB`, `1.5 MB`, `2.0 GB`."""
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{round(size / 1024)} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


def attached_line(part: Mapping[str, Any]) -> str:
    """The prompt line of a workspace file, like `Attached: <path> (xlsx, 240 KB)`.

    The type is the file extension, or the content type when the name has
    none.
    """
    path = str(part.get("path") or "")
    stem, dot, extension = path.rsplit("/", 1)[-1].rpartition(".")
    mime = str(part.get("mime_type") or "")
    if stem and dot and extension:
        kind = extension.lower()
    elif mime and mime != "application/octet-stream":
        kind = mime
    else:
        kind = "file"
    size = part.get("size_bytes")
    details = f"{kind}, {size_text(size)}" if isinstance(size, int) else kind
    return f"Attached: {path} ({details})"


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


def env_credentials_from_context(
    context: Mapping[str, Any],
) -> list[ContextEnvCredential]:
    """The environment credentials of a parsed turn context, typed."""
    raw = context.get("env_credentials")
    if not isinstance(raw, list):
        return []
    return [
        ContextEnvCredential.model_validate(item)
        for item in raw
        if isinstance(item, Mapping)
    ]


def env_credential_hosts(credentials: list[ContextEnvCredential]) -> tuple[str, ...]:
    """The union of `allowed_hosts`, lowercased, in first-seen order."""
    hosts: list[str] = []
    for credential in credentials:
        for host in credential.allowed_hosts:
            key = host.lower()
            if key not in hosts:
                hosts.append(key)
    return tuple(hosts)
