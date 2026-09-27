import uuid
from typing import Any

from apipi.config import Settings
from apipi.env.spec import EnvironmentSpec, environment_payload
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.store.repo import get_file, get_skill, get_vault
from apipi.worker.pi.sandbox import (
    SANDBOX_IMAGE_KEY,
    SANDBOX_SIZE_KEY,
    require_image_size,
    require_known_image,
    resolve_sandbox_image,
    resolve_sandbox_size,
)

_HOSTED = "openai_hosted"


def canonical_env_type(value: str | None) -> str | None:
    if value == "hosted":
        return _HOSTED
    return value


def sandbox_defaults_from_metadata(metadata: object) -> dict[str, Any] | None:
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            return None
    if not isinstance(metadata, dict):
        return None
    size = metadata.get(SANDBOX_SIZE_KEY)
    image = metadata.get(SANDBOX_IMAGE_KEY)
    if not isinstance(size, str) and not isinstance(image, str):
        return None
    environment: dict[str, Any] = {"type": _HOSTED}
    if isinstance(size, str):
        environment["sandbox_size"] = size
    if isinstance(image, str):
        environment["sandbox_image"] = image
    return {"environment": environment}


def normalize_sandbox_aliases(
    metadata: dict[str, Any] | None,
    session_defaults: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    meta = dict(metadata) if metadata else {}
    defaults = dict(session_defaults) if session_defaults else None
    env = _environment_dict(defaults)
    meta_size = meta.get(SANDBOX_SIZE_KEY) if SANDBOX_SIZE_KEY in meta else None
    meta_image = meta.get(SANDBOX_IMAGE_KEY) if SANDBOX_IMAGE_KEY in meta else None
    env_size = env.get("sandbox_size") if env else None
    env_image = env.get("sandbox_image") if env else None
    if _both_differ(meta_size, env_size):
        raise ApiError(
            "invalid_request",
            "apipi.sandbox_size does not match "
            "session_defaults.environment.sandbox_size",
            code="invalid_request",
        )
    if _both_differ(meta_image, env_image):
        raise ApiError(
            "invalid_request",
            "apipi.sandbox_image does not match "
            "session_defaults.environment.sandbox_image",
            code="invalid_request",
        )
    size = env_size if isinstance(env_size, str) else meta_size
    image = env_image if isinstance(env_image, str) else meta_image
    if not isinstance(size, str) and not isinstance(image, str):
        return (meta or None) if metadata is not None else None, defaults
    if env is None:
        env = {"type": _HOSTED}
    if isinstance(size, str):
        env["sandbox_size"] = size
        meta[SANDBOX_SIZE_KEY] = size
    if isinstance(image, str):
        env["sandbox_image"] = image
        meta[SANDBOX_IMAGE_KEY] = image
    if defaults is None:
        defaults = {}
    defaults["environment"] = env
    return meta, defaults


def mirror_sandbox_metadata(
    metadata: dict[str, Any] | None,
    session_defaults: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    env = _environment_dict(session_defaults)
    if env is None:
        return meta
    size = env.get("sandbox_size")
    image = env.get("sandbox_image")
    if isinstance(size, str):
        meta[SANDBOX_SIZE_KEY] = size
    if isinstance(image, str):
        meta[SANDBOX_IMAGE_KEY] = image
    return meta


def strip_sandbox_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    meta = dict(metadata or {})
    meta.pop(SANDBOX_SIZE_KEY, None)
    meta.pop(SANDBOX_IMAGE_KEY, None)
    return meta


def validate_defaults_shape(
    settings: Settings, defaults: dict[str, Any] | None
) -> None:
    env = _environment_dict(defaults)
    if env is None:
        return
    spec = EnvironmentSpec.model_validate(env)
    payload = environment_payload(spec)
    if "sandbox_size" not in payload and "sandbox_image" not in payload:
        return
    size = resolve_sandbox_size(
        environment_size=payload.get("sandbox_size")
        if isinstance(payload.get("sandbox_size"), str)
        else None,
        session_metadata=None,
        agent_metadata=None,
        default=settings.sandbox_default_size,
    )
    image = resolve_sandbox_image(
        environment_image=payload.get("sandbox_image")
        if isinstance(payload.get("sandbox_image"), str)
        else None,
        session_metadata=None,
        agent_metadata=None,
        size=size,
        default=settings.sandbox_default_image,
    )
    require_known_image(settings, image)
    require_image_size(image, size)


async def require_default_refs(
    db: Any,
    tenant_id: uuid.UUID,
    defaults: dict[str, Any] | None,
    *,
    agent_label: str | None = None,
    dangling: bool = False,
) -> None:
    if not defaults:
        return
    env = _environment_dict(defaults) or {}
    for item in env.get("skills") or []:
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        if not isinstance(skill_id, str) or not skill_id:
            continue
        if await get_skill(db, tenant_id, skill_id) is None:
            _missing(agent_label, "skill", skill_id, dangling=dangling)
    for item in env.get("files") or []:
        if not isinstance(item, dict) or item.get("type") != "file_id":
            continue
        file_id = item.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            continue
        if await get_file(db, tenant_id, file_id) is None:
            _missing(agent_label, "file", file_id, dangling=dangling)
    for raw in defaults.get("vault_ids") or []:
        vault_id = _as_uuid(raw)
        if vault_id is None or await get_vault(db, tenant_id, vault_id) is None:
            _missing(agent_label, "vault", str(raw), dangling=dangling)


def merge_session_create(
    *,
    agent_defaults: dict[str, Any] | None,
    environment: EnvironmentSpec | None,
    vault_ids: list[uuid.UUID] | None,
    inherit: bool,
) -> tuple[EnvironmentSpec | None, list[uuid.UUID] | None, str | None, str | None]:
    if not inherit or not agent_defaults:
        return environment, vault_ids, None, None
    agent_env = _environment_dict(agent_defaults)
    agent_size, agent_image = _sandbox_pair(agent_env)
    agent_type = canonical_env_type(agent_env.get("type")) if agent_env else None
    session_type = (
        canonical_env_type(environment.type) if environment is not None else None
    )
    apply_fields = session_type is None or (
        agent_type is not None and session_type == agent_type
    )
    merged_env = _merge_environment(
        agent_env if apply_fields else None,
        environment,
        session_type=session_type,
        agent_type=agent_type if apply_fields else None,
    )
    merged_vaults = _union_ids(agent_defaults.get("vault_ids"), vault_ids)
    return merged_env, merged_vaults, agent_size, agent_image


def _merge_environment(
    agent_env: dict[str, Any] | None,
    session: EnvironmentSpec | None,
    *,
    session_type: str | None,
    agent_type: str | None,
) -> EnvironmentSpec | None:
    if agent_env is None:
        return session
    base = {
        key: value
        for key, value in agent_env.items()
        if key not in {"sandbox_size", "sandbox_image"} and value is not None
    }
    if agent_type is not None:
        base["type"] = agent_type
    if session is None:
        if "type" not in base:
            return None
        return EnvironmentSpec.model_validate(base)
    overlay = session.model_dump(exclude_none=True)
    if session_type is not None:
        base["type"] = session_type
    for key in ("capability_directories", "setup_commands", "network"):
        if key in overlay:
            base[key] = overlay[key]
    if "packages" in overlay:
        base["packages"] = _merge_maps(base.get("packages"), overlay["packages"])
    if "env" in overlay:
        base["env"] = _merge_maps(base.get("env"), overlay["env"])
    if "files" in overlay:
        base["files"] = _merge_files(base.get("files"), overlay["files"])
    if "skills" in overlay:
        base["skills"] = _merge_skills(base.get("skills"), overlay["skills"])
    if "sandbox_size" in overlay:
        base["sandbox_size"] = overlay["sandbox_size"]
    if "sandbox_image" in overlay:
        base["sandbox_image"] = overlay["sandbox_image"]
    if "type" not in base:
        return session
    return EnvironmentSpec.model_validate(base)


def _merge_maps(agent_value: object, session_value: object) -> dict[str, Any]:
    base = dict(agent_value) if isinstance(agent_value, dict) else {}
    if isinstance(session_value, dict):
        for key, value in session_value.items():
            if value is not None:
                base[key] = value
    return base


def _merge_files(agent_files: object, session_files: object) -> list[Any]:
    by_path: dict[str, Any] = {}
    order: list[str] = []
    for item in _as_list(agent_files) + _as_list(session_files):
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str):
            continue
        if path not in by_path:
            order.append(path)
        by_path[path] = item
    return [by_path[path] for path in order]


def _merge_skills(agent_skills: object, session_skills: object) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for item in _as_list(agent_skills) + _as_list(session_skills):
        if not isinstance(item, dict):
            continue
        skill_id = item.get("skill_id")
        if not isinstance(skill_id, str) or skill_id in seen:
            continue
        seen.add(skill_id)
        out.append(item)
    return out


def _union_ids(
    agent_ids: object, session_ids: list[uuid.UUID] | None
) -> list[uuid.UUID] | None:
    out: list[uuid.UUID] = []
    seen: set[str] = set()
    for raw in _as_list(agent_ids) + list(session_ids or []):
        parsed = _as_uuid(raw)
        if parsed is None or str(parsed) in seen:
            continue
        seen.add(str(parsed))
        out.append(parsed)
    if not out and session_ids is None:
        return None
    return out


def _environment_dict(defaults: dict[str, Any] | None) -> dict[str, Any] | None:
    if not defaults:
        return None
    env = defaults.get("environment")
    if not isinstance(env, dict):
        return None
    return {key: value for key, value in env.items() if value is not None}


def _sandbox_pair(env: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not env:
        return None, None
    size = env.get("sandbox_size")
    image = env.get("sandbox_image")
    return (
        size if isinstance(size, str) else None,
        image if isinstance(image, str) else None,
    )


def _both_differ(left: object, right: object) -> bool:
    return isinstance(left, str) and isinstance(right, str) and left != right


def _as_list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_uuid(value: object) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def _missing(agent_label: str | None, kind: str, ref: str, *, dangling: bool) -> None:
    if not dangling:
        not_found()
    who = agent_label or "agent"
    raise ApiError(
        "invalid_request",
        f"{who} session_defaults references missing {kind} {ref}",
        code="invalid_request",
    )
