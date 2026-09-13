import asyncio
import shutil
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Protocol

from apipi.config import ConfigError, Settings
from apipi.pi.dirs import blob_user, sessions_root


def blob_key(
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID,
) -> str:
    return f"{tenant_id}/{blob_user(key_id)}/{session_id}/{artifact_id}"


def blob_prefix(tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID) -> str:
    return f"{tenant_id}/{blob_user(key_id)}/{session_id}/"


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


class LocalBlobs:
    def __init__(self, settings: Settings) -> None:
        self._root = sessions_root(settings) / ".artifacts"

    def _path(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> Path:
        path = self._root / blob_key(tenant_id, key_id, session_id, artifact_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _dir(self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID) -> Path:
        return self._root / blob_prefix(tenant_id, key_id, session_id).rstrip("/")

    async def put(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        data: bytes,
    ) -> None:
        self._path(tenant_id, key_id, session_id, artifact_id).write_bytes(data)

    async def get(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> bytes | None:
        path = self._path(tenant_id, key_id, session_id, artifact_id)
        if not path.is_file():
            return None
        return path.read_bytes()

    async def delete(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> None:
        path = self._path(tenant_id, key_id, session_id, artifact_id)
        if path.is_file():
            path.unlink()

    async def delete_session(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> None:
        path = self._dir(tenant_id, key_id, session_id)
        if path.is_dir():
            shutil.rmtree(path)

    async def used_bytes(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> int:
        path = self._dir(tenant_id, key_id, session_id)
        if not path.is_dir():
            return 0
        total = 0
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
        return total


class MemoryBlobs:
    def __init__(self, objects: MutableMapping[str, bytes] | None = None) -> None:
        self.objects: MutableMapping[str, bytes] = (
            objects if objects is not None else {}
        )

    def _key(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> str:
        return blob_key(tenant_id, key_id, session_id, artifact_id)

    async def put(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        data: bytes,
    ) -> None:
        self.objects[self._key(tenant_id, key_id, session_id, artifact_id)] = data

    async def get(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> bytes | None:
        return self.objects.get(self._key(tenant_id, key_id, session_id, artifact_id))

    async def delete(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> None:
        self.objects.pop(self._key(tenant_id, key_id, session_id, artifact_id), None)

    async def delete_session(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> None:
        prefix = blob_prefix(tenant_id, key_id, session_id)
        for key in [item for item in self.objects if item.startswith(prefix)]:
            del self.objects[key]

    async def used_bytes(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> int:
        prefix = blob_prefix(tenant_id, key_id, session_id)
        return sum(
            len(data) for key, data in self.objects.items() if key.startswith(prefix)
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


def _s3_object_key(
    settings: Settings,
    tenant_id: uuid.UUID,
    key_id: str,
    session_id: uuid.UUID,
    artifact_id: uuid.UUID | None = None,
) -> str:
    prefix = settings.s3_prefix.strip().strip("/")
    body = (
        blob_key(tenant_id, key_id, session_id, artifact_id)
        if artifact_id is not None
        else blob_prefix(tenant_id, key_id, session_id)
    )
    if prefix:
        return f"{prefix}/{body}"
    return body


class S3Blobs:
    def __init__(self, settings: Settings, client: object | None = None) -> None:
        self._settings = settings
        self._bucket = settings.s3_bucket or ""
        self._client: Any = client if client is not None else _make_s3_client(settings)

    def _key(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> str:
        return _s3_object_key(
            self._settings, tenant_id, key_id, session_id, artifact_id
        )

    def _session_prefix(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> str:
        return _s3_object_key(self._settings, tenant_id, key_id, session_id)

    async def put(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
        data: bytes,
    ) -> None:
        key = self._key(tenant_id, key_id, session_id, artifact_id)
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=key, Body=data
        )

    async def get(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> bytes | None:
        key = self._key(tenant_id, key_id, session_id, artifact_id)
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

    async def delete(
        self,
        tenant_id: uuid.UUID,
        key_id: str,
        session_id: uuid.UUID,
        artifact_id: uuid.UUID,
    ) -> None:
        key = self._key(tenant_id, key_id, session_id, artifact_id)
        await asyncio.to_thread(
            self._client.delete_object, Bucket=self._bucket, Key=key
        )

    async def delete_session(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> None:
        prefix = self._session_prefix(tenant_id, key_id, session_id)
        keys = await asyncio.to_thread(self._list_keys, prefix)
        for key in keys:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )

    async def used_bytes(
        self, tenant_id: uuid.UUID, key_id: str, session_id: uuid.UUID
    ) -> int:
        prefix = self._session_prefix(tenant_id, key_id, session_id)
        return await asyncio.to_thread(self._sum_sizes, prefix)

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


def blob_store(settings: Settings) -> ArtifactBlobs:
    if settings.artifact_store == "s3":
        return S3Blobs(settings)
    return LocalBlobs(settings)
