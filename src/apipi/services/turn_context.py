"""Build the turn context the API sends with worker commands.

The builder runs on the API, where the database, the vault and the
object store are available. It resolves everything the worker needs
for one turn (session, agent, effective idle TTL, files, skills, the
Pi session blob, HTTP MCP servers and the model key) into a plain
dict that validates as `apipi.worker.turn_context.TurnContext`.

File bytes never enter the context. With ``APIPI_ARTIFACT_STORE=s3``
each file, skill and Pi session blob becomes a presigned GET URL with
a short TTL. With the filesystem store each reference becomes a path
relative to the shared store root (``APIPI_LOCAL_STORE_DIR``, falling
back to ``APIPI_SESSIONS_DIR``), which the worker reads directly; the
API and the worker must see the same filesystem.
"""

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from apipi.config import Settings
from apipi.env.setup import file_id_refs_from, skill_refs_from
from apipi.gateway.auth import not_found
from apipi.services.agents import definition_for_session
from apipi.store.blobs import (
    NS_ARTIFACTS,
    NS_FILES,
    NS_SKILLS,
    Namespace,
    ObjectStore,
    ObjectStoreError,
    blob_key,
    file_object_id,
    local_object_path,
    object_store,
    skill_object_id,
)
from apipi.store.engine import Store
from apipi.store.repo import get_file, get_session, get_skill
from apipi.worker.pi.dirs import store_root
from apipi.worker.pi.idle import resolve_idle_ttl
from apipi.worker.turn_context import TurnContext

PRESIGN_TTL = timedelta(minutes=15)


def _store_error(message: str, *, operation: str, key: str = "") -> ObjectStoreError:
    return ObjectStoreError(
        message, operation=operation, bucket="", key=key, code="artifact_store"
    )


def local_ref_path(settings: Settings, local_path: str) -> Path:
    """Resolve a context relative path inside the shared store root."""
    root = store_root(settings).resolve()
    candidate = (root / local_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise _store_error(
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
            raise _store_error(
                f"cannot fetch turn context ref: {exc}", operation="get"
            ) from exc
        if response.status_code != 200:
            raise _store_error(
                f"cannot fetch turn context ref: HTTP {response.status_code}",
                operation="get",
                key=url,
            )
        return response.content
    local_path = ref.get("local_path")
    if isinstance(local_path, str) and local_path:
        path = local_ref_path(settings, local_path)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise _store_error(
                f"cannot read turn context ref: {exc}",
                operation="get",
                key=local_path,
            ) from exc
    raise _store_error("turn context ref has no url or local_path", operation="get")


def _store_ref(
    settings: Settings,
    namespace: Namespace,
    object_id: str,
    *,
    objects: ObjectStore | None = None,
) -> dict[str, str | None]:
    if settings.artifact_store == "s3":
        backend = objects if objects is not None else object_store(settings)
        presign = getattr(backend, "presign", None)
        if presign is None:
            raise _store_error(
                "object store cannot presign GET URLs",
                operation="presign",
                key=object_id,
            )
        url, _headers = presign("GET", namespace, object_id, expires=PRESIGN_TTL)
        return {"url": url, "local_path": None}
    root = store_root(settings)
    relative = local_object_path(root, namespace, object_id).relative_to(root)
    return {"url": None, "local_path": str(relative)}


async def build_turn_context(
    store: Store,
    settings: Settings,
    tenant_id: Any,
    session_id: Any,
    *,
    mcp_servers: list[Any] | None = None,
    api_key: str | None = None,
    key_id: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    base_url: str | None = None,
    objects: ObjectStore | None = None,
) -> dict[str, Any]:
    """Resolve one turn's context from the database and the object store."""
    from apipi.services.runtime import (
        _effective_builtin_tools,
        _effective_codemode,
        _function_tools,
    )
    from apipi.worker.pi.settings_json import resolve_thinking

    async with store.session() as db:
        row = await get_session(db, tenant_id, session_id)
        if row is None:
            not_found()
        environment = row.environment if isinstance(row.environment, dict) else {}
        session_metadata = (
            row.metadata_json if isinstance(row.metadata_json, dict) else {}
        )
        agent_metadata: dict[str, Any] = {}
        model = row.model
        instructions = row.instructions
        function_tools: list[dict[str, Any]] = []
        agent_idle: str | None = None
        if row.agent_id is not None:
            definition = await definition_for_session(db, tenant_id, row)
            if definition is not None:
                raw_tools = definition.get("tools")
                raw = raw_tools if isinstance(raw_tools, list) else []
                function_tools = _function_tools(raw)
                raw_model = definition.get("model")
                model = raw_model if isinstance(raw_model, str) else model
                raw_instructions = definition.get("instructions")
                instructions = (
                    raw_instructions
                    if isinstance(raw_instructions, str)
                    else instructions
                )
                meta = definition.get("metadata")
                agent_metadata = meta if isinstance(meta, dict) else {}
                raw_idle = definition.get("idle_ttl")
                agent_idle = raw_idle if isinstance(raw_idle, str) else None
        builtin_tools = _effective_builtin_tools(
            environment, session_metadata, agent_metadata
        )
        thinking = resolve_thinking(settings, session_metadata, agent_metadata)
        env_type = environment.get("type")
        ttl = resolve_idle_ttl(
            settings,
            env_type if isinstance(env_type, str) else None,
            session_idle=row.idle_ttl,
            session_metadata=session_metadata,
            agent_idle=agent_idle,
        )
        files: list[dict[str, Any]] = []
        for path, file_id in file_id_refs_from(environment):
            file_row = await get_file(db, tenant_id, file_id)
            if file_row is None:
                not_found()
            object_id = file_object_id(tenant_id, file_id)
            ref = _store_ref(settings, NS_FILES, object_id, objects=objects)
            files.append(
                {
                    "path": path,
                    "object_id": object_id,
                    "url": ref["url"],
                    "local_path": ref["local_path"],
                    "size_bytes": file_row.size,
                    "content_type": file_row.content_type,
                }
            )
        skills: list[dict[str, Any]] = []
        for skill_id in skill_refs_from(environment):
            skill_row = await get_skill(db, tenant_id, skill_id)
            if skill_row is None:
                not_found()
            object_id = skill_object_id(tenant_id, skill_id)
            ref = _store_ref(settings, NS_SKILLS, object_id, objects=objects)
            skills.append(
                {
                    "skill_id": skill_id,
                    "object_id": object_id,
                    "url": ref["url"],
                    "local_path": ref["local_path"],
                }
            )
        pi_session: dict[str, Any] = {"present": False}
        if row.pi_session_id is not None:
            object_id = blob_key(tenant_id, row.key_id, row.id, row.pi_session_id)
            ref = _store_ref(settings, NS_ARTIFACTS, object_id, objects=objects)
            pi_session = {
                "present": True,
                "object_id": object_id,
                "url": ref["url"],
                "local_path": ref["local_path"],
            }
        resolved_key_id = key_id if key_id is not None else row.key_id
        context = {
            "session": {
                "environment": environment,
                "metadata": session_metadata,
                "required_actions": (
                    row.required_actions
                    if isinstance(row.required_actions, list)
                    else []
                ),
                "status": row.status,
                "user_id": user_id if user_id is not None else row.user_id,
                "org_id": org_id if org_id is not None else row.org_id,
                "key_id": resolved_key_id,
                "agent_id": str(row.agent_id) if row.agent_id is not None else None,
                "idle_ttl_seconds": (ttl.total_seconds() if ttl is not None else None),
            },
            "agent": {
                "model": model,
                "instructions": instructions,
                "function_tools": function_tools,
                "metadata": agent_metadata,
                "builtin_tools": builtin_tools,
                "codemode": _effective_codemode(
                    builtin_tools, session_metadata, agent_metadata
                ),
                "thinking": thinking,
            },
            "model": {
                "base_url": (
                    base_url if base_url is not None else settings.model_base_url
                ),
                "api_key": api_key,
            },
            "mcp": [_serialize_mcp_server(server) for server in (mcp_servers or [])],
            "files": files,
            "skills": skills,
            "pi_session": pi_session,
        }
    return TurnContext.model_validate(context).model_dump()


async def materialize_workspace_files(
    files: list[Any], settings: Settings | None
) -> list[tuple[str, bytes]]:
    """Fetch workspace file bytes for context references without DB access."""
    if not files or settings is None:
        return []
    materialized: list[tuple[str, bytes]] = []
    for ref in files:
        if not isinstance(ref, Mapping):
            continue
        path = ref.get("path")
        if not isinstance(path, str):
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
        raise _store_error(
            "cannot restore the Pi session without settings", operation="get"
        )
    return await fetch_ref_bytes(pi_session, settings)


def _serialize_mcp_server(server: Any) -> dict[str, Any]:
    """Serialize one resolved HTTP MCP server for the command context."""
    headers = getattr(server, "headers", {})
    allowed = getattr(server, "allowed_tools", ())
    return {
        "server_label": str(getattr(server, "server_label", "")),
        "server_url": str(getattr(server, "server_url", "")),
        "headers": dict(headers) if isinstance(headers, dict) else {},
        "allowed_tools": [str(item) for item in allowed],
    }


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
