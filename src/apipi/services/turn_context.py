"""Build the turn context the API sends with worker commands.

The builder runs on the API, where the database, the vault and the
object store are available. It resolves everything the worker needs
for one turn (session, agent, effective idle TTL, files, skills, the
Pi session blob, HTTP MCP servers and the model key) into a plain
dict that validates as `apipi.protocol.TurnContext`.

File bytes never enter the context. With ``APIPI_ARTIFACT_STORE=s3``
each file, skill and Pi session blob becomes a presigned GET URL with
a short TTL. With the filesystem store each reference becomes a path
relative to the shared store root (``APIPI_LOCAL_STORE_DIR``, falling
back to ``APIPI_SESSIONS_DIR``), which the worker reads directly; the
API and the worker must see the same filesystem.
"""

import logging
from datetime import timedelta
from typing import Any

from apipi.common.dirs import store_root
from apipi.common.errors import store_error
from apipi.common.idle import resolve_idle_ttl
from apipi.common.logutil import log_event
from apipi.common.objects import (
    NS_ARTIFACTS,
    NS_FILES,
    NS_SKILLS,
    Namespace,
    local_object_path,
)
from apipi.config import Settings
from apipi.env.setup import file_id_refs_from, skill_refs_from
from apipi.gateway.auth import not_found
from apipi.gateway.content import InputFile
from apipi.protocol import InputFileRef, InputImageRef, TurnContext
from apipi.services.agents import definition_for_session
from apipi.services.search import SearchResolver, web_search_tool
from apipi.store.blobs import (
    ObjectStore,
    blob_key,
    file_object_id,
    object_store,
    skill_object_id,
)
from apipi.store.engine import Store
from apipi.store.repo import get_file, get_session, get_skill

PRESIGN_TTL = timedelta(minutes=15)

log = logging.getLogger("apipi.search")


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
            raise store_error(
                "object store cannot presign GET URLs",
                operation="presign",
                key=object_id,
            )
        url, _headers = presign("GET", namespace, object_id, expires=PRESIGN_TTL)
        return {"url": url, "local_path": None}
    root = store_root(settings)
    relative = local_object_path(root, namespace, object_id).relative_to(root)
    return {"url": None, "local_path": str(relative)}


def input_image_ref(
    settings: Settings,
    tenant_id: Any,
    file_id: str,
    *,
    mime_type: str,
    size_bytes: int,
    objects: ObjectStore | None = None,
) -> dict[str, Any]:
    """One `image` part of `turn.start`: a store reference, never the bytes."""
    object_id = file_object_id(tenant_id, file_id)
    ref = _store_ref(settings, NS_FILES, object_id, objects=objects)
    part = InputImageRef(
        file_id=file_id,
        object_id=object_id,
        mime_type=mime_type,
        size_bytes=size_bytes,
    )
    if ref["url"] is not None:
        part.url = ref["url"]
    if ref["local_path"] is not None:
        part.local_path = ref["local_path"]
    return part.to_wire()


def input_file_ref(
    settings: Settings,
    tenant_id: Any,
    file: InputFile,
    *,
    objects: ObjectStore | None = None,
) -> dict[str, Any]:
    """One `file` part of `turn.start`: a store reference, never the bytes."""
    object_id = file_object_id(tenant_id, file.file_id)
    ref = _store_ref(settings, NS_FILES, object_id, objects=objects)
    part = InputFileRef(
        file_id=file.file_id,
        filename=file.filename,
        object_id=object_id,
        mime_type=file.mime or "application/octet-stream",
        size_bytes=file.size,
        model_input=file.model_input,
    )
    if ref["url"] is not None:
        part.url = ref["url"]
    if ref["local_path"] is not None:
        part.local_path = ref["local_path"]
    return part.to_wire()


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
    search: SearchResolver | None = None,
) -> dict[str, Any]:
    """Resolve one turn's context from the database and the object store."""
    from apipi.common.pi_metadata import (
        effective_builtin_tools,
        effective_codemode,
        resolve_thinking,
    )
    from apipi.common.pi_metadata import (
        function_tools as pick_function_tools,
    )

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
        effective_tools: Any = row.tools
        if row.agent_id is not None:
            definition = await definition_for_session(db, tenant_id, row)
            if definition is not None:
                raw_tools = definition.get("tools")
                raw = raw_tools if isinstance(raw_tools, list) else []
                effective_tools = raw
                function_tools = pick_function_tools(raw)
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
        web_search = False
        if web_search_tool(effective_tools) is not None:
            resolver = search if search is not None else SearchResolver(settings)
            target = await resolver.resolve(
                tenant_id,
                user_id if user_id is not None else row.user_id,
                org_id if org_id is not None else row.org_id,
            )
            web_search = target is not None
            if target is None:
                log_event(
                    log,
                    logging.WARNING,
                    "web_search tool not loaded for this turn",
                    event="search.denied",
                    error_code="search_denied",
                    session_id=str(session_id),
                    tenant_id=str(tenant_id),
                    reason="resolver",
                )
        builtin_tools = effective_builtin_tools(
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
                "codemode": effective_codemode(
                    builtin_tools, session_metadata, agent_metadata
                ),
                "thinking": thinking,
                "web_search": web_search,
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
