import asyncio
import shutil
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Literal, Protocol

from apipi.config import ConfigError, Settings
from apipi.worker.pi.dirs import blob_user, sessions_root

Namespace = Literal["artifacts", "files", "skills"]

NS_ARTIFACTS: Namespace = "artifacts"
NS_FILES: Namespace = "files"
NS_SKILLS: Namespace = "skills"


def blob_key(
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> str:
    return f"{tenant_id}/{blob_user(key_id)}/{session_id}/{artifact_id}"


def blob_prefix(tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID) -> str:
    return f"{tenant_id}/{blob_user(key_id)}/{session_id}/"


def file_object_id(tenant_id: uuid.UUID, file_id: str) -> str:
    return f"{tenant_id}/{_object_id(file_id)}"


def skill_object_id(tenant_id: uuid.UUID, skill_id: str) -> str:
    return f"{tenant_id}/{_object_id(skill_id)}"


def s3_namespace_prefix(settings: Settings, namespace: Namespace) -> str:
    prefix = settings.s3_prefix.strip().strip("/")
    if namespace == NS_ARTIFACTS:
        return prefix
    parent, _sep, last = prefix.rpartition("/")
    if last == NS_ARTIFACTS:
        return f"{parent}/{namespace}" if parent else namespace
    if prefix:
        return f"{prefix}/{namespace}"
    return namespace


def s3_object_key(settings: Settings, namespace: Namespace, object_id: str) -> str:
    prefix = s3_namespace_prefix(settings, namespace)
    body = _object_id(object_id)
    if prefix:
        return f"{prefix}/{body}"
    return body


def s3_prefix_key(settings: Settings, namespace: Namespace, prefix: str) -> str:
    head = s3_namespace_prefix(settings, namespace)
    body = prefix.strip().strip("/")
    if body:
        body = _object_id(body) + "/"
    if head and body:
        return f"{head}/{body}"
    if head:
        return f"{head}/"
    return body


def local_object_path(root: Path, namespace: Namespace, object_id: str) -> Path:
    body = _object_id(object_id)
    if namespace == NS_ARTIFACTS:
        return root / ".artifacts" / body
    return root / ".store" / namespace / body


def _object_id(object_id: str) -> str:
    text = object_id.strip().strip("/")
    if not text:
        raise ValueError("object id is required")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid object id")
    return text


class ObjectStore(Protocol):
    async def put(
        self,
        namespace: Namespace,
        object_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None: ...

    async def get(self, namespace: Namespace, object_id: str) -> bytes | None: ...

    async def delete(self, namespace: Namespace, object_id: str) -> None: ...

    async def delete_prefix(self, namespace: Namespace, prefix: str) -> None: ...

    async def used_bytes(self, namespace: Namespace, prefix: str) -> int: ...


class ArtifactBlobs(Protocol):
    async def put(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        data: bytes,
    ) -> None: ...

    async def get(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> bytes | None: ...

    async def delete(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> None: ...

    async def delete_session(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> None: ...

    async def used_bytes(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> int: ...


class LocalStore:
    def __init__(self, settings: Settings) -> None:
        self._root = sessions_root(settings)

    def _path(self, namespace: Namespace, object_id: str) -> Path:
        return local_object_path(self._root, namespace, object_id)

    def _dir(self, namespace: Namespace, prefix: str) -> Path:
        return local_object_path(self._root, namespace, prefix.rstrip("/") or prefix)

    async def put(
        self,
        namespace: Namespace,
        object_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        del content_type
        path = self._path(namespace, object_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, namespace: Namespace, object_id: str) -> bytes | None:
        path = self._path(namespace, object_id)
        if not path.is_file():
            return None
        return path.read_bytes()

    async def delete(self, namespace: Namespace, object_id: str) -> None:
        path = self._path(namespace, object_id)
        if path.is_file():
            path.unlink()

    async def delete_prefix(self, namespace: Namespace, prefix: str) -> None:
        path = self._dir(namespace, prefix)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()

    async def used_bytes(self, namespace: Namespace, prefix: str) -> int:
        path = self._dir(namespace, prefix)
        if not path.is_dir():
            return 0
        total = 0
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
        return total


class MemoryStore:
    def __init__(self, objects: MutableMapping[str, bytes] | None = None) -> None:
        self.objects: MutableMapping[str, bytes] = (
            objects if objects is not None else {}
        )

    def _key(self, namespace: Namespace, object_id: str) -> str:
        return f"{namespace}/{_object_id(object_id)}"

    def _prefix(self, namespace: Namespace, prefix: str) -> str:
        body = prefix.strip().strip("/")
        if body:
            return f"{namespace}/{_object_id(body)}/"
        return f"{namespace}/"

    async def put(
        self,
        namespace: Namespace,
        object_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        del content_type
        self.objects[self._key(namespace, object_id)] = data

    async def get(self, namespace: Namespace, object_id: str) -> bytes | None:
        return self.objects.get(self._key(namespace, object_id))

    async def delete(self, namespace: Namespace, object_id: str) -> None:
        self.objects.pop(self._key(namespace, object_id), None)

    async def delete_prefix(self, namespace: Namespace, prefix: str) -> None:
        head = self._prefix(namespace, prefix)
        for key in [item for item in self.objects if item.startswith(head)]:
            del self.objects[key]

    async def used_bytes(self, namespace: Namespace, prefix: str) -> int:
        head = self._prefix(namespace, prefix)
        return sum(
            len(data) for key, data in self.objects.items() if key.startswith(head)
        )


class S3Store:
    def __init__(self, settings: Settings, client: object | None = None) -> None:
        self._settings = settings
        self._bucket = settings.s3_bucket or ""
        self._client: Any = client if client is not None else _make_s3_client(settings)

    def _key(self, namespace: Namespace, object_id: str) -> str:
        return s3_object_key(self._settings, namespace, object_id)

    def _prefix(self, namespace: Namespace, prefix: str) -> str:
        return s3_prefix_key(self._settings, namespace, prefix)

    async def put(
        self,
        namespace: Namespace,
        object_id: str,
        data: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        key = self._key(namespace, object_id)
        kwargs: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        await asyncio.to_thread(self._client.put_object, **kwargs)

    async def get(self, namespace: Namespace, object_id: str) -> bytes | None:
        key = self._key(namespace, object_id)
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
        except Exception as exc:
            if _s3_missing(exc):
                return None
            raise
        body = response.get("Body")
        if body is None:
            return None
        read = getattr(body, "read", None)
        if read is None:
            return None
        data = await asyncio.to_thread(read)
        return data if isinstance(data, bytes) else None

    async def delete(self, namespace: Namespace, object_id: str) -> None:
        key = self._key(namespace, object_id)
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=key
        )

    async def delete_prefix(self, namespace: Namespace, prefix: str) -> None:
        head = self._prefix(namespace, prefix)
        keys = await asyncio.to_thread(self._list_keys, head)
        for key in keys:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )

    async def used_bytes(self, namespace: Namespace, prefix: str) -> int:
        head = self._prefix(namespace, prefix)
        return await asyncio.to_thread(self._sum_sizes, head)

    def _list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                key = item.get("Key")
                if isinstance(key, str):
                    keys.append(key)
            if not response.get("IsTruncated"):
                return keys
            nxt = response.get("NextContinuationToken")
            token = nxt if isinstance(nxt, str) else None
            if token is None:
                return keys

    def _sum_sizes(self, prefix: str) -> int:
        total = 0
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self._client.list_objects_v2(**kwargs)
            for item in response.get("Contents") or []:
                size = item.get("Size")
                if isinstance(size, int):
                    total += size
            if not response.get("IsTruncated"):
                return total
            nxt = response.get("NextContinuationToken")
            token = nxt if isinstance(nxt, str) else None
            if token is None:
                return total


class ArtifactAdapter:
    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    async def put(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        data: bytes,
    ) -> None:
        await self._store.put(
            NS_ARTIFACTS, blob_key(tenant_id, key_id, session_id, artifact_id), data
        )

    async def get(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> bytes | None:
        return await self._store.get(
            NS_ARTIFACTS, blob_key(tenant_id, key_id, session_id, artifact_id)
        )

    async def delete(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> None:
        await self._store.delete(
            NS_ARTIFACTS, blob_key(tenant_id, key_id, session_id, artifact_id)
        )

    async def delete_session(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> None:
        await self._store.delete_prefix(
            NS_ARTIFACTS, blob_prefix(tenant_id, key_id, session_id)
        )

    async def used_bytes(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> int:
        return await self._store.used_bytes(
            NS_ARTIFACTS, blob_prefix(tenant_id, key_id, session_id)
        )


class LocalBlobs(ArtifactAdapter):
    def __init__(self, settings: Settings, store: ObjectStore | None = None) -> None:
        super().__init__(store if store is not None else LocalStore(settings))


class MemoryBlobs(ArtifactAdapter):
    def __init__(
        self,
        objects: MutableMapping[str, bytes] | None = None,
        store: MemoryStore | None = None,
    ) -> None:
        resolved = store if store is not None else MemoryStore(objects)
        super().__init__(resolved)
        self.objects = resolved.objects


class S3Blobs(ArtifactAdapter):
    def __init__(
        self,
        settings: Settings,
        client: object | None = None,
        store: ObjectStore | None = None,
    ) -> None:
        super().__init__(
            store if store is not None else S3Store(settings, client=client)
        )


def s3_addressing(settings: Settings) -> str:
    if settings.s3_addressing == "path":
        return "path"
    if settings.s3_addressing == "virtual":
        return "virtual"
    if settings.s3_endpoint:
        return "path"
    return "virtual"


def s3_client_kwargs(settings: Settings) -> dict[str, object]:
    style = s3_addressing(settings)
    config_kwargs: dict[str, object] = {
        "s3": {"addressing_style": style},
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    kwargs: dict[str, object] = {
        "service_name": "s3",
        "region_name": settings.s3_region,
        "config_kwargs": config_kwargs,
    }
    if settings.s3_endpoint:
        kwargs["endpoint_url"] = settings.s3_endpoint
    return kwargs


def _s3_missing(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    if not isinstance(error, dict):
        return False
    code = error.get("Code")
    return code in {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}


def _make_s3_client(settings: Settings) -> object:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise ConfigError(
            "APIPI_ARTIFACT_STORE=s3 requires boto3 (uv sync --extra s3)"
        ) from exc
    kwargs = s3_client_kwargs(settings)
    config_kwargs = kwargs.pop("config_kwargs")
    if not isinstance(config_kwargs, dict):
        config_kwargs = {}
    try:
        config = Config(**config_kwargs)
    except TypeError:
        config = Config(s3=config_kwargs.get("s3", {"addressing_style": "path"}))
    client_kwargs: dict[str, object] = {
        "region_name": kwargs.get("region_name"),
        "config": config,
    }
    endpoint = kwargs.get("endpoint_url")
    if endpoint is not None:
        client_kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **client_kwargs)


def object_store(settings: Settings) -> ObjectStore:
    if settings.artifact_store == "s3":
        return S3Store(settings)
    return LocalStore(settings)


def blob_store(settings: Settings) -> ArtifactBlobs:
    store = object_store(settings)
    if settings.artifact_store == "s3":
        return S3Blobs(settings, store=store)
    return LocalBlobs(settings, store=store)
