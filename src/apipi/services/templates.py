import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from apipi import __version__
from apipi.config import Settings
from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.gateway.schemas import StrictModel
from apipi.services.agents import AgentService, AgentWrite
from apipi.services.bundles import (
    apply_bundle,
    build_bundle,
    read_bundle,
)
from apipi.services.files import FileService
from apipi.services.skill_store import SkillService
from apipi.store.blobs import (
    NS_FILES,
    NS_SKILLS,
    NS_TEMPLATES,
    ObjectStore,
    ObjectStoreError,
    S3Store,
    file_object_id,
    skill_object_id,
    template_object_id,
)
from apipi.store.engine import Store
from apipi.store.models import TemplateRow
from apipi.store.repo import (
    create_template,
    delete_template,
    get_credential_by_id,
    get_file,
    get_skill,
    get_template,
    get_vault,
    list_templates,
)
from apipi.worker.pi.model_host import require_saved_model
from apipi.worker.pi.sandbox import require_image_size, require_known_image

_PROVENANCE_ID = "apipi.template_id"
_PROVENANCE_AT = "apipi.template_updated_at"


class TemplateCreate(StrictModel):
    agent_id: uuid.UUID
    name: str | None = None
    description: str | None = None


class TemplateAgentCreate(StrictModel):
    secrets: dict[str, str] | None = None
    credentials: dict[str, str] | None = None
    overrides: dict[str, str] | None = None


def new_template_id() -> str:
    return f"tpl_{uuid.uuid4().hex}"


def template_body(
    row: TemplateRow, requires: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "schema_version": row.schema_version,
        "visibility": row.visibility,
        "created_by": row.created_by,
        "size": row.size,
        "sha256": row.sha256,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }
    if requires is not None:
        body["requires"] = requires
    return body


class TemplateService:
    def __init__(
        self,
        store: Store,
        objects: ObjectStore,
        settings: Settings,
        agents: AgentService,
        files: FileService,
        skills: SkillService,
    ) -> None:
        self.store = store
        self.objects = objects
        self.settings = settings
        self.agents = agents
        self.files = files
        self.skills = skills

    async def create_from_agent(
        self,
        tenant_id: uuid.UUID,
        body: TemplateCreate,
        *,
        created_by: str | None,
    ) -> dict[str, Any]:
        agent = await self.agents.get(tenant_id, body.agent_id)
        data, manifest, warnings = await self._bundle_from_agent(
            tenant_id,
            agent,
            name=body.name,
            description=body.description,
        )
        return await self._store_bundle(
            tenant_id,
            data,
            manifest,
            name=body.name or _template_name(manifest),
            description=body.description or _template_description(manifest),
            created_by=created_by,
            warnings=warnings,
        )

    async def import_bundle(
        self,
        tenant_id: uuid.UUID,
        data: bytes,
        *,
        name: str | None,
        description: str | None,
        created_by: str | None,
    ) -> dict[str, Any]:
        parsed = read_bundle(data, max_bytes=int(self.settings.max_file_bytes))
        warnings = list(parsed["warnings"])
        warnings.extend(self._image_warnings(parsed["manifest"]))
        self._validate_agent_shape(parsed)
        return await self._store_bundle(
            tenant_id,
            data,
            parsed["manifest"],
            name=name or _template_name(parsed["manifest"]),
            description=description or _template_description(parsed["manifest"]),
            created_by=created_by,
            warnings=warnings,
        )

    async def list_objects(self, tenant_id: uuid.UUID) -> dict[str, Any]:
        async with self.store.session() as db:
            rows = await list_templates(db, tenant_id)
        return {"data": [template_body(row) for row in rows]}

    async def get(self, tenant_id: uuid.UUID, template_id: str) -> dict[str, Any]:
        row, parsed = await self._load(tenant_id, template_id)
        requires = parsed["manifest"].get("requires")
        if not isinstance(requires, dict):
            requires = {"secrets": [], "credentials": []}
        return template_body(row, requires)

    async def download(
        self, tenant_id: uuid.UUID, template_id: str
    ) -> tuple[str, bytes | str]:
        row, _parsed = await self._load(tenant_id, template_id)
        filename = f"{row.name or row.id}.apipi-agent.zip"
        if isinstance(self.objects, S3Store):
            url, _headers = self.objects.presign(
                "GET",
                NS_TEMPLATES,
                row.object_id,
                expires=self.settings.presign_ttl,
                content_type="application/zip",
                filename=filename,
            )
            return filename, url
        data = await self.objects.get(NS_TEMPLATES, row.object_id)
        if data is None:
            not_found()
        return filename, data

    async def delete(self, tenant_id: uuid.UUID, template_id: str) -> dict[str, Any]:
        async with self.store.session() as db:
            row = await get_template(db, tenant_id, template_id)
            if row is None:
                not_found()
            object_id = row.object_id
            await delete_template(db, tenant_id, template_id)
        await self.objects.delete(NS_TEMPLATES, object_id)
        return {"id": template_id, "deleted": True}

    async def export_agent(
        self, tenant_id: uuid.UUID, agent_id: uuid.UUID
    ) -> tuple[str, bytes]:
        agent = await self.agents.get(tenant_id, agent_id)
        data, _manifest, _warnings = await self._bundle_from_agent(
            tenant_id, agent, name=agent.get("name"), description=None
        )
        name = agent.get("name") if isinstance(agent.get("name"), str) else agent_id.hex
        return f"{name}.apipi-agent.zip", data

    async def create_agent(
        self,
        tenant_id: uuid.UUID,
        template_id: str,
        body: TemplateAgentCreate,
        *,
        api_key: str | None,
    ) -> dict[str, Any]:
        row, parsed = await self._load(tenant_id, template_id)
        tool_ids, vault_ids = await self._split_mappings(tenant_id, body.credentials)
        created_skills: list[dict[str, Any]] = []
        created_files: list[str] = []
        try:
            skill_ids, created_skills = await self._copy_skills(tenant_id, parsed)
            file_specs, created_files = await self._copy_files(tenant_id, parsed)
            agent_body, missing, warnings = apply_bundle(
                parsed,
                secrets=body.secrets,
                credentials=tool_ids,
                vaults=vault_ids,
                overrides=body.overrides,
                skill_ids=skill_ids,
                file_specs=file_specs,
            )
            warnings.extend(self._image_warnings(parsed["manifest"], strict=True))
            self._stamp_provenance(agent_body, row)
            model_warning = await self._note_model(agent_body.get("model"), api_key)
            if model_warning:
                if model_warning.startswith("missing:"):
                    missing["models"].append(model_warning.removeprefix("missing:"))
                else:
                    warnings.append(model_warning)
            created = await self.agents.create(
                tenant_id,
                AgentWrite.model_validate(agent_body),
                api_key=api_key,
                check_model=False,
            )
        except Exception:
            await self._rollback(tenant_id, created_skills, created_files)
            raise
        return {
            "agent": created,
            "skills": created_skills,
            "missing": missing,
            "warnings": warnings,
        }

    async def _bundle_from_agent(
        self,
        tenant_id: uuid.UUID,
        agent: dict[str, Any],
        *,
        name: str | None,
        description: str | None,
    ) -> tuple[bytes, dict[str, Any], list[str]]:
        skills, files, credentials, vaults = await self._gather(tenant_id, agent)
        data, manifest, warnings = build_bundle(
            agent=agent,
            template_name=name,
            template_description=description,
            exported_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            apipi_version=__version__,
            skills=skills,
            files=files,
            credentials=credentials,
            vaults=vaults,
        )
        read_bundle(data, max_bytes=int(self.settings.max_file_bytes))
        return data, manifest, warnings

    async def _gather(
        self, tenant_id: uuid.UUID, agent: dict[str, Any]
    ) -> tuple[
        dict[str, bytes],
        dict[str, bytes],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        skills: dict[str, bytes] = {}
        files: dict[str, bytes] = {}
        credentials: dict[str, dict[str, Any]] = {}
        vaults: dict[str, dict[str, Any]] = {}
        defaults = agent.get("session_defaults")
        env = defaults.get("environment") if isinstance(defaults, dict) else None
        async with self.store.session() as db:
            if isinstance(env, dict):
                for item in env.get("skills") or []:
                    if not isinstance(item, dict):
                        continue
                    skill_id = item.get("skill_id")
                    if not isinstance(skill_id, str):
                        continue
                    row = await get_skill(db, tenant_id, skill_id)
                    if row is None:
                        not_found()
                    blob = await self.objects.get(
                        NS_SKILLS, skill_object_id(tenant_id, skill_id)
                    )
                    if blob is None:
                        not_found()
                    item["name"] = row.name
                    skills[f"skills/{row.name}.zip"] = blob
                for item in env.get("files") or []:
                    if not isinstance(item, dict):
                        continue
                    path = item.get("path")
                    if not isinstance(path, str):
                        continue
                    inline = item.get("type") == "inline" and isinstance(
                        item.get("data"), str
                    )
                    if inline:
                        import base64

                        files[f"files/{path}"] = base64.b64decode(item["data"])
                    elif item.get("type") == "file_id":
                        file_id = item.get("file_id")
                        if not isinstance(file_id, str):
                            continue
                        row = await get_file(db, tenant_id, file_id)
                        if row is None:
                            not_found()
                        blob = await self.objects.get(
                            NS_FILES, file_object_id(tenant_id, file_id)
                        )
                        if blob is None:
                            not_found()
                        files[f"files/{path}"] = blob
                raw_vaults = (
                    defaults.get("vault_ids") if isinstance(defaults, dict) else None
                )
                for raw in raw_vaults or []:
                    try:
                        vault_id = uuid.UUID(str(raw))
                    except ValueError:
                        continue
                    vault = await get_vault(db, tenant_id, vault_id)
                    if vault is None:
                        not_found()
                    vaults[str(vault_id)] = {"name": vault.name or str(vault_id)}
            for tool in agent.get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                raw_id = tool.get("credential_id")
                if not isinstance(raw_id, str):
                    continue
                try:
                    credential_id = uuid.UUID(raw_id)
                except ValueError:
                    continue
                row = await get_credential_by_id(db, tenant_id, credential_id)
                if row is None:
                    not_found()
                credentials[raw_id] = {
                    "name": row.name or f"credential_{raw_id[:8]}",
                    "kind": row.auth_type,
                    "mcp_server_url": row.mcp_server_url,
                }
        return skills, files, credentials, vaults

    async def _store_bundle(
        self,
        tenant_id: uuid.UUID,
        data: bytes,
        manifest: dict[str, Any],
        *,
        name: str | None,
        description: str | None,
        created_by: str | None,
        warnings: list[str],
    ) -> dict[str, Any]:
        template_id = new_template_id()
        object_id = template_object_id(tenant_id, template_id)
        try:
            await self.objects.put(
                NS_TEMPLATES,
                object_id,
                data,
                content_type="application/zip",
            )
        except ObjectStoreError as exc:
            raise ApiError(
                "api_error",
                "Artifact store unavailable",
                code="artifact_store",
                status_code=503,
            ) from exc
        version = manifest.get("schema_version")
        schema_version = version if isinstance(version, str) else "1.0"
        async with self.store.session() as db:
            row = await create_template(
                db,
                tenant_id,
                template_id=template_id,
                created_by=created_by,
                name=name,
                description=description,
                schema_version=schema_version,
                object_id=object_id,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            requires = manifest.get("requires")
            body = template_body(row, requires if isinstance(requires, dict) else None)
        body["warnings"] = warnings
        return body

    async def _load(
        self, tenant_id: uuid.UUID, template_id: str
    ) -> tuple[TemplateRow, dict[str, Any]]:
        async with self.store.session() as db:
            row = await get_template(db, tenant_id, template_id)
            if row is None:
                not_found()
            object_id = row.object_id
        data = await self.objects.get(NS_TEMPLATES, object_id)
        if data is None:
            not_found()
        return row, read_bundle(data, max_bytes=int(self.settings.max_file_bytes))

    async def _copy_skills(
        self, tenant_id: uuid.UUID, parsed: dict[str, Any]
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        created: list[dict[str, Any]] = []
        ids: dict[str, str] = {}
        for path, blob in parsed["skills"].items():
            body = await self.skills.create(tenant_id, data=blob, filename=path)
            created.append(body)
            ids[path] = str(body["id"])
        return ids, created

    async def _copy_files(
        self, tenant_id: uuid.UUID, parsed: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        import base64

        from apipi.services.bundles import INLINE_FILE_LIMIT

        specs: list[dict[str, Any]] = []
        created: list[str] = []
        for path, blob in parsed["files"].items():
            rel = path.removeprefix("files/")
            if len(blob) <= INLINE_FILE_LIMIT:
                specs.append(
                    {
                        "type": "inline",
                        "path": rel,
                        "data": base64.b64encode(blob).decode("ascii"),
                    }
                )
                continue
            body = await self.files.create(
                tenant_id,
                data=blob,
                filename=rel.rsplit("/", 1)[-1],
                purpose="user_data",
            )
            created.append(str(body["id"]))
            specs.append({"type": "file_id", "file_id": body["id"], "path": rel})
        return specs, created

    async def _rollback(
        self,
        tenant_id: uuid.UUID,
        skills: list[dict[str, Any]],
        file_ids: list[str],
    ) -> None:
        for skill in skills:
            skill_id = skill.get("id")
            if isinstance(skill_id, str):
                await self.skills.delete(tenant_id, skill_id)
        for file_id in file_ids:
            await self.files.delete(tenant_id, file_id)

    async def _split_mappings(
        self, tenant_id: uuid.UUID, credentials: dict[str, str] | None
    ) -> tuple[dict[str, str], dict[str, str]]:
        tool_ids: dict[str, str] = {}
        vault_ids: dict[str, str] = {}
        if not credentials:
            return tool_ids, vault_ids
        async with self.store.session() as db:
            for name, raw in credentials.items():
                try:
                    parsed_id = uuid.UUID(raw)
                except ValueError as exc:
                    raise ApiError(
                        "invalid_request",
                        f"credential mapping {name} was not found",
                        code="invalid_request",
                    ) from exc
                credential = await get_credential_by_id(db, tenant_id, parsed_id)
                vault = await get_vault(db, tenant_id, parsed_id)
                if credential is None and vault is None:
                    raise ApiError(
                        "invalid_request",
                        f"credential mapping {name} was not found",
                        code="invalid_request",
                    )
                if credential is not None:
                    tool_ids[name] = str(credential.id)
                    vault_ids[name] = str(credential.vault_id)
                elif vault is not None:
                    vault_ids[name] = str(vault.id)
        return tool_ids, vault_ids

    async def _note_model(self, model: object, api_key: str | None) -> str | None:
        if not isinstance(model, str) or not model:
            return None
        try:
            await require_saved_model(self.settings, model, api_key)
        except ApiError as exc:
            if exc.code == "model_not_found":
                return f"missing:{model}"
            if exc.code in {"model_host_unreachable", "model_host_unauthorized"}:
                return "model host could not be listed"
            raise
        return None

    def _image_warnings(
        self, manifest: dict[str, Any], *, strict: bool = False
    ) -> list[str]:
        image = manifest.get("image")
        if not isinstance(image, dict):
            return []
        image_id = image.get("id")
        size = image.get("size")
        warnings: list[str] = []
        try:
            if isinstance(image_id, str):
                require_known_image(self.settings, image_id)
            if isinstance(image_id, str) and isinstance(size, str):
                require_image_size(image_id, size)
        except ApiError as exc:
            if strict:
                raise
            warnings.append(exc.message)
        return warnings

    def _validate_agent_shape(self, parsed: dict[str, Any]) -> None:
        from apipi.services.bundles import apply_bundle as apply

        body, _missing, _warnings = apply(
            parsed,
            secrets={name: "x" for name in _secret_names(parsed)},
            credentials={name: str(uuid.uuid4()) for name in _credential_names(parsed)},
            overrides=None,
            skill_ids={
                path: f"skill-{index}" for index, path in enumerate(parsed["skills"])
            },
            file_specs=[
                {"type": "inline", "path": path.removeprefix("files/"), "data": "eA=="}
                for path in parsed["files"]
            ],
        )
        AgentWrite.model_validate(body)
        defaults = body.get("session_defaults")
        if isinstance(defaults, dict) and isinstance(defaults.get("environment"), dict):
            from apipi.env.spec import EnvironmentSpec, environment_payload

            environment_payload(EnvironmentSpec.model_validate(defaults["environment"]))
        from apipi.services.chat_tools import (
            is_chat_profile,
            reject_disallowed_chat_tools,
        )
        from apipi.worker.pi.idle import normalize_idle_ttl, validate_idle_metadata
        from apipi.worker.pi.settings_json import validate_pi_metadata

        validate_pi_metadata(body.get("metadata"))
        validate_idle_metadata(body.get("metadata"))
        if isinstance(body.get("idle_ttl"), str):
            normalize_idle_ttl(body["idle_ttl"])
        if is_chat_profile(body.get("metadata")):
            reject_disallowed_chat_tools(body.get("tools"))

    def _stamp_provenance(self, agent_body: dict[str, Any], row: TemplateRow) -> None:
        metadata = agent_body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata[_PROVENANCE_ID] = row.id
        metadata[_PROVENANCE_AT] = row.updated_at.isoformat()
        agent_body["metadata"] = metadata


def _template_name(manifest: dict[str, Any]) -> str | None:
    template = manifest.get("template")
    if isinstance(template, dict) and isinstance(template.get("name"), str):
        return template["name"]
    agent = manifest.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("name"), str):
        return agent["name"]
    return None


def _template_description(manifest: dict[str, Any]) -> str | None:
    template = manifest.get("template")
    if isinstance(template, dict) and isinstance(template.get("description"), str):
        return template["description"]
    return None


def _secret_names(parsed: dict[str, Any]) -> list[str]:
    requires = parsed["manifest"].get("requires")
    if not isinstance(requires, dict):
        return []
    return [
        item["name"]
        for item in requires.get("secrets") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]


def _credential_names(parsed: dict[str, Any]) -> list[str]:
    requires = parsed["manifest"].get("requires")
    if not isinstance(requires, dict):
        return []
    return [
        item["name"]
        for item in requires.get("credentials") or []
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
