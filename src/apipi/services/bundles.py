import hashlib
import io
import json
import re
import stat
import zipfile
from typing import Any

from apipi.env.spec import EnvironmentSpec
from apipi.gateway.errors import ApiError
from apipi.services.skills import inspect_skill_zip

SCHEMA_VERSION = "1.0"
KIND = "apipi.agent"
MAX_ENTRIES = 256
MAX_RATIO = 100
INLINE_FILE_LIMIT = 256 * 1024
_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_PORTABLE = frozenset(
    {
        "apipi.thinking",
        "apipi.system_prompt",
        "apipi.idle_ttl",
        "apipi.session_kind",
    }
)
_DROP = frozenset(
    {
        "apipi.actor_type",
        "apipi.schedule_id",
        "apipi.source",
        "apipi.template_id",
        "apipi.template_updated_at",
        "apipi.sandbox_size",
        "apipi.sandbox_image",
    }
)
_AGENT_FIELDS = frozenset(
    {
        "name",
        "model",
        "instructions",
        "idle_ttl",
        "metadata",
        "tools",
        "session_defaults",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "exported_at",
        "exported_by",
        "template",
        "agent",
        "image",
        "requires",
    }
)


def bundle_error(message: str, code: str = "invalid_request") -> ApiError:
    return ApiError("invalid_request", message, code=code)


def read_bundle(data: bytes, *, max_bytes: int) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise bundle_error("bundle is too large", code="payload_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise bundle_error("bundle must be a zip") from exc
    _reject_unsafe(archive, max_bytes=max_bytes)
    try:
        raw = archive.read("agent.json")
    except KeyError as exc:
        raise bundle_error("bundle needs agent.json") from exc
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise bundle_error("agent.json must be utf-8 JSON") from exc
    if not isinstance(manifest, dict):
        raise bundle_error("agent.json must be an object")
    warnings = _check_manifest(manifest)
    skills = _embedded(archive, "skills", suffix=".zip")
    files = _embedded(archive, "files", suffix=None)
    for path, blob in skills.items():
        try:
            inspect_skill_zip(blob)
        except Exception as exc:
            message = getattr(exc, "message", None) or str(exc)
            raise bundle_error(f"{path} is not a valid skill: {message}") from exc
    image_warning = _image_warning(manifest.get("image"))
    if image_warning:
        warnings.append(image_warning)
    return {
        "manifest": manifest,
        "skills": skills,
        "files": files,
        "warnings": warnings,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }


def build_bundle(
    *,
    agent: dict[str, Any],
    template_name: str | None,
    template_description: str | None,
    exported_at: str,
    apipi_version: str,
    skills: dict[str, bytes],
    files: dict[str, bytes],
    credentials: dict[str, dict[str, Any]],
    vaults: dict[str, dict[str, Any]],
) -> tuple[bytes, dict[str, Any], list[str]]:
    redacted, requires, warnings = redact_agent(
        agent, credentials=credentials, vaults=vaults, skills=skills, files=files
    )
    image = _image_block(redacted)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "exported_at": exported_at,
        "exported_by": {"apipi_version": apipi_version},
        "template": {
            "name": template_name or redacted.get("name") or "agent",
            "description": template_description or "",
        },
        "agent": redacted,
        "requires": requires,
    }
    if image is not None:
        manifest["image"] = image
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("agent.json", json.dumps(manifest, indent=2) + "\n")
        for path, blob in sorted(skills.items()):
            archive.writestr(path, blob)
        for path, blob in sorted(files.items()):
            archive.writestr(path, blob)
    data = buf.getvalue()
    return data, manifest, warnings


def redact_agent(
    agent: dict[str, Any],
    *,
    credentials: dict[str, dict[str, Any]],
    vaults: dict[str, dict[str, Any]],
    skills: dict[str, bytes],
    files: dict[str, bytes],
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    warnings: list[str] = []
    secrets: list[dict[str, str]] = []
    credential_rows: list[dict[str, str]] = []
    out: dict[str, Any] = {}
    for key in ("name", "model", "instructions", "idle_ttl"):
        if agent.get(key) is not None:
            out[key] = agent[key]
    raw_metadata = agent.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    kept, dropped = _portable_metadata(metadata)
    warnings.extend(dropped)
    if kept:
        out["metadata"] = kept
    raw_tools = agent.get("tools")
    tools = raw_tools if isinstance(raw_tools, list) else []
    out["tools"] = [
        _redact_tool(tool, secrets, credential_rows, credentials) for tool in tools
    ]
    defaults = agent.get("session_defaults")
    if isinstance(defaults, dict):
        out["session_defaults"] = _redact_defaults(
            defaults,
            secrets,
            credential_rows,
            credentials,
            vaults,
            skills,
            files,
        )
    requires = {"secrets": secrets, "credentials": credential_rows}
    return out, requires, warnings


def apply_bundle(
    parsed: dict[str, Any],
    *,
    secrets: dict[str, str] | None,
    credentials: dict[str, str] | None,
    vaults: dict[str, str] | None = None,
    overrides: dict[str, str] | None,
    skill_ids: dict[str, str],
    file_specs: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, list[str]], list[str]]:
    manifest = parsed["manifest"]
    agent = manifest.get("agent")
    if not isinstance(agent, dict):
        raise bundle_error("agent.json needs an agent object")
    warnings = list(parsed.get("warnings") or [])
    body, extra = _known_agent(agent)
    warnings.extend(extra)
    supplied_secrets = secrets or {}
    supplied_credentials = credentials or {}
    missing: dict[str, list[str]] = {"models": [], "secrets": [], "credentials": []}
    body["tools"] = [
        _apply_tool(tool, supplied_secrets, supplied_credentials, missing)
        for tool in body.get("tools") or []
    ]
    defaults = body.get("session_defaults")
    if isinstance(defaults, dict):
        body["session_defaults"] = _apply_defaults(
            defaults,
            supplied_secrets,
            vaults if vaults is not None else supplied_credentials,
            missing,
            skill_ids,
            file_specs,
        )
    if overrides:
        if overrides.get("name"):
            body["name"] = overrides["name"]
        if overrides.get("model"):
            body["model"] = overrides["model"]
    raw_metadata = body.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    ignored = [
        key for key in metadata if key.startswith("apipi.") and key not in _PORTABLE
    ]
    if ignored:
        warnings.append("ignored metadata: " + ", ".join(sorted(ignored)))
        body["metadata"] = {
            key: value for key, value in metadata.items() if key not in ignored
        }
    return body, missing, warnings


def comparable_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    agent = manifest.get("agent") if isinstance(manifest.get("agent"), dict) else {}
    return {
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "agent": agent,
        "requires": manifest.get("requires"),
        "image": manifest.get("image"),
    }


def _check_manifest(manifest: dict[str, Any]) -> list[str]:
    version = manifest.get("schema_version")
    if not isinstance(version, str) or "." not in version:
        raise bundle_error(
            "schema_version is missing", code="bundle_version_unsupported"
        )
    major_text, _minor, *_rest = version.split(".")
    try:
        major = int(major_text)
    except ValueError as exc:
        raise bundle_error(
            "schema_version is not supported", code="bundle_version_unsupported"
        ) from exc
    if major > 1:
        raise bundle_error(
            f"schema_version {version} is newer than 1.x",
            code="bundle_version_unsupported",
        )
    if major < 1:
        raise bundle_error(
            f"schema_version {version} is not supported",
            code="bundle_version_unsupported",
        )
    if manifest.get("kind") != KIND:
        raise bundle_error("kind must be apipi.agent")
    warnings = [
        f"ignored field {key}" for key in manifest if key not in _MANIFEST_FIELDS
    ]
    return warnings


def _reject_unsafe(archive: zipfile.ZipFile, *, max_bytes: int) -> None:
    seen: set[str] = set()
    count = 0
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        count += 1
        if count > MAX_ENTRIES:
            raise bundle_error("bundle has too many files")
        name = info.filename
        if "\\" in name or name.startswith("/") or _has_drive(name):
            raise bundle_error("bundle path is not allowed")
        if _is_symlink(info):
            raise bundle_error("bundle must not contain symlinks")
        parts = [part for part in name.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise bundle_error("bundle path is not allowed")
        rel = "/".join(parts)
        if rel in seen:
            raise bundle_error(f"duplicate bundle path {rel}")
        seen.add(rel)
        if not _known_path(rel):
            raise bundle_error(f"bundle path is not allowed: {rel}")
        total += info.file_size
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise bundle_error("bundle compression ratio is too high")
        if total > max_bytes * MAX_RATIO:
            raise bundle_error("bundle uncompressed size is too large")


def _known_path(rel: str) -> bool:
    if rel in {"agent.json", "README.md"}:
        return True
    if rel.startswith("skills/") and rel.endswith(".zip") and rel.count("/") == 1:
        return True
    return rel.startswith("files/") and rel != "files/" and ".." not in rel.split("/")


def _embedded(
    archive: zipfile.ZipFile, prefix: str, *, suffix: str | None
) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/").lstrip("/")
        if not name.startswith(prefix + "/"):
            continue
        if suffix is not None and not name.endswith(suffix):
            continue
        out[name] = archive.read(info)
    return out


def _image_warning(image: object) -> str | None:
    if image is None:
        return None
    if not isinstance(image, dict):
        return "ignored image"
    return None


def _image_block(agent: dict[str, Any]) -> dict[str, Any] | None:
    defaults = agent.get("session_defaults")
    if not isinstance(defaults, dict):
        return None
    env = defaults.get("environment")
    if not isinstance(env, dict):
        return None
    image = env.get("sandbox_image")
    size = env.get("sandbox_size")
    if not isinstance(image, str) and not isinstance(size, str):
        return None
    block: dict[str, Any] = {}
    if isinstance(image, str):
        block["id"] = image
    if isinstance(size, str):
        block["size"] = size
    return block


def _portable_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    kept: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in metadata.items():
        if key.startswith("apipi.") and key not in _PORTABLE:
            if key not in _DROP:
                warnings.append(f"dropped metadata {key}")
            continue
        kept[key] = value
    return kept, warnings


def _redact_tool(
    tool: object,
    secrets: list[dict[str, str]],
    credentials: list[dict[str, str]],
    known: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    out = dict(tool)
    label = str(out.get("server_label") or "mcp")
    headers = out.get("headers")
    if isinstance(headers, dict):
        redacted: dict[str, Any] = {}
        for header, value in headers.items():
            if not isinstance(value, str):
                continue
            name = _secret_name(label, str(header), value)
            redacted[str(header)] = {"$secret": name}
            secrets.append(
                {
                    "name": name,
                    "used_by": f"agent.tools.headers.{header}",
                }
            )
        out["headers"] = redacted
    credential_id = out.get("credential_id")
    if isinstance(credential_id, str) and credential_id:
        info = known.get(credential_id, {})
        name = str(info.get("name") or f"credential_{credential_id[:8]}")
        out.pop("credential_id", None)
        out["credential"] = {
            "$credential": name,
            "kind": info.get("kind") or "static_bearer",
            "mcp_server_url": info.get("mcp_server_url") or "",
        }
        credentials.append(
            {
                "name": name,
                "kind": str(info.get("kind") or "static_bearer"),
                "used_by": "agent.tools",
            }
        )
    return out


def _redact_defaults(
    defaults: dict[str, Any],
    secrets: list[dict[str, str]],
    credentials: list[dict[str, str]],
    known: dict[str, dict[str, Any]],
    vaults: dict[str, dict[str, Any]],
    skills: dict[str, bytes],
    files: dict[str, bytes],
) -> dict[str, Any]:
    out = dict(defaults)
    env = out.get("environment")
    if isinstance(env, dict):
        copied = dict(env)
        raw_env = copied.get("env")
        if isinstance(raw_env, dict):
            replaced: dict[str, Any] = {}
            for key, value in raw_env.items():
                name = str(key)
                replaced[name] = {"$secret": name}
                secrets.append(
                    {
                        "name": name,
                        "used_by": f"agent.session_defaults.environment.env.{name}",
                    }
                )
                del value
            copied["env"] = replaced
        raw_files = copied.get("files")
        if isinstance(raw_files, list):
            copied["files"] = [
                _file_ref(item, files) for item in raw_files if isinstance(item, dict)
            ]
        raw_skills = copied.get("skills")
        if isinstance(raw_skills, list):
            copied["skills"] = [
                _skill_ref(item, skills)
                for item in raw_skills
                if isinstance(item, dict)
            ]
        out["environment"] = copied
    vault_ids = out.get("vault_ids")
    if isinstance(vault_ids, list):
        replaced_ids = []
        for raw in vault_ids:
            text = str(raw)
            info = vaults.get(text, {})
            name = str(info.get("name") or f"vault_{text[:8]}")
            replaced_ids.append({"$credential": name})
            credentials.append(
                {
                    "name": name,
                    "kind": "vault",
                    "used_by": "agent.session_defaults.vault_ids",
                }
            )
        out["vault_ids"] = replaced_ids
    del known
    return out


def _file_ref(item: dict[str, Any], files: dict[str, bytes]) -> dict[str, Any]:
    path = str(item.get("path") or "file")
    bundle_path = f"files/{path}"
    return {"path": path, "bundle_path": bundle_path}


def _skill_ref(item: dict[str, Any], skills: dict[str, bytes]) -> dict[str, Any]:
    skill_id = str(item.get("skill_id") or "")
    name = str(item.get("name") or skill_id or "skill")
    bundle_path = f"skills/{name}.zip"
    blob = skills.get(bundle_path, b"")
    return {
        "name": name,
        "bundle_path": bundle_path,
        "sha256": hashlib.sha256(blob).hexdigest(),
    }


def _apply_tool(
    tool: object,
    secrets: dict[str, str],
    credentials: dict[str, str],
    missing: dict[str, list[str]],
) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {}
    out = {key: value for key, value in tool.items() if key != "credential"}
    headers = out.get("headers")
    if isinstance(headers, dict):
        applied: dict[str, str] = {}
        for header, value in headers.items():
            secret = _placeholder_name(value, "$secret")
            if secret is None:
                if isinstance(value, str):
                    applied[str(header)] = value
                continue
            if secret not in secrets:
                missing["secrets"].append(secret)
                continue
            applied[str(header)] = secrets[secret]
        if applied:
            out["headers"] = applied
        else:
            out.pop("headers", None)
    credential = tool.get("credential")
    name = _placeholder_name(credential, "$credential")
    if name is not None:
        mapped = credentials.get(name)
        if mapped:
            out["credential_id"] = mapped
        else:
            missing["credentials"].append(name)
    return out


def _apply_defaults(
    defaults: dict[str, Any],
    secrets: dict[str, str],
    credentials: dict[str, str],
    missing: dict[str, list[str]],
    skill_ids: dict[str, str],
    file_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    out = dict(defaults)
    env = out.get("environment")
    if isinstance(env, dict):
        copied = dict(env)
        raw_env = copied.get("env")
        if isinstance(raw_env, dict):
            applied: dict[str, str] = {}
            for key, value in raw_env.items():
                secret = _placeholder_name(value, "$secret")
                if secret is None:
                    if isinstance(value, str):
                        applied[str(key)] = value
                    continue
                if secret not in secrets:
                    missing["secrets"].append(secret)
                    continue
                applied[str(key)] = secrets[secret]
            if applied:
                copied["env"] = applied
            else:
                copied.pop("env", None)
        copied["skills"] = [
            {"type": "skill_reference", "skill_id": skill_id}
            for skill_id in skill_ids.values()
        ]
        copied["files"] = file_specs
        out["environment"] = copied
    vault_ids = out.get("vault_ids")
    if isinstance(vault_ids, list):
        mapped_ids: list[str] = []
        for item in vault_ids:
            name = _placeholder_name(item, "$credential")
            if name is None:
                continue
            mapped = credentials.get(name)
            if mapped:
                mapped_ids.append(mapped)
            else:
                missing["credentials"].append(name)
        if mapped_ids:
            out["vault_ids"] = mapped_ids
        else:
            out.pop("vault_ids", None)
    return out


def _known_agent(agent: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    warnings = [
        f"ignored agent field {key}" for key in agent if key not in _AGENT_FIELDS
    ]
    body = {key: agent[key] for key in agent if key in _AGENT_FIELDS}
    return body, warnings


def _secret_name(label: str, header: str, value: str) -> str:
    match = _ENV_REF.fullmatch(value.strip())
    if match:
        return match.group(1)
    raw = f"{label}_{header}"
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_").upper()
    return cleaned or "SECRET"


def _placeholder_name(value: object, key: str) -> str | None:
    if isinstance(value, dict) and isinstance(value.get(key), str):
        return str(value[key])
    return None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _has_drive(name: str) -> bool:
    return len(name) > 2 and name[1] == ":"


def env_spec_or_none(value: object) -> EnvironmentSpec | None:
    if not isinstance(value, dict):
        return None
    return EnvironmentSpec.model_validate(value)
