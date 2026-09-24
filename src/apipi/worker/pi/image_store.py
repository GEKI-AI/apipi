import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from apipi.config import ConfigError, Settings
from apipi.store.blobs import make_s3_client

S3_MISSING = "s3 image source requires boto3 (uv sync --extra s3)"


@dataclass(frozen=True)
class ImageURI:
    scheme: str
    bucket: str
    prefix: str
    path: str


def parse_image_uri(uri: str) -> ImageURI:
    parsed = urlparse(uri)
    scheme = parsed.scheme
    if scheme == "file":
        path = parsed.path
        if parsed.netloc and parsed.netloc != "localhost":
            path = f"/{parsed.netloc}{parsed.path}"
        if not path.startswith("/"):
            raise ConfigError(f"file image source must be absolute: {uri}")
        return ImageURI(scheme="file", bucket="", prefix="", path=path)
    if scheme == "s3":
        bucket = parsed.netloc
        prefix = parsed.path.lstrip("/")
        if not bucket:
            raise ConfigError(f"s3 image source needs a bucket: {uri}")
        return ImageURI(scheme="s3", bucket=bucket, prefix=prefix.rstrip("/"), path="")
    if scheme == "https":
        return ImageURI(scheme="https", bucket="", prefix="", path=uri)
    raise ConfigError(f"image source must be s3://, https://, or file://: {uri}")


def _safe_name(name: str) -> str:
    if not name or name.startswith("/") or ".." in Path(name).parts:
        raise ConfigError(f"image object name must be relative: {name}")
    return name


def _s3_missing(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error")
    if not isinstance(error, dict):
        return False
    return error.get("Code") in {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}


class FileImageStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, name: str) -> Path:
        return self.root / _safe_name(name)

    def exists(self, name: str) -> bool:
        return self._path(name).is_file()

    def get(self, name: str) -> bytes:
        path = self._path(name)
        if not path.is_file():
            raise ConfigError(f"image store is missing {name}")
        return path.read_bytes()

    def put_bytes(self, name: str, data: bytes) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def put_file(self, name: str, source: Path) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        shutil.copyfile(source, tmp)
        tmp.replace(path)


class S3ImageStore:
    def __init__(
        self,
        settings: Settings,
        uri: ImageURI,
        client: object | None = None,
    ) -> None:
        self.bucket = uri.bucket
        self.prefix = uri.prefix
        if client is None:
            client = make_s3_client(settings, missing=S3_MISSING)
        self.client: Any = client

    def key(self, name: str) -> str:
        safe = _safe_name(name)
        if not self.prefix:
            return safe
        return f"{self.prefix}/{safe}"

    def exists(self, name: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            if _s3_missing(exc):
                return False
            raise
        return True

    def get(self, name: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            if _s3_missing(exc):
                raise ConfigError(f"image store is missing {name}") from exc
            raise
        body = response["Body"]
        data = body.read()
        if not isinstance(data, bytes):
            raise ConfigError(f"image store returned no bytes for {name}")
        return data

    def put_bytes(self, name: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self.key(name), Body=data)

    def put_file(self, name: str, source: Path) -> None:
        upload = getattr(self.client, "upload_file", None)
        if callable(upload):
            upload(str(source), self.bucket, self.key(name))
            return
        self.put_bytes(name, source.read_bytes())


class HttpImageStore:
    def __init__(self, base: str, client: httpx.Client | None = None) -> None:
        self.base = base.rstrip("/")
        if client is None:
            client = httpx.Client(follow_redirects=True)
        self.client = client

    def _url(self, name: str) -> str:
        return f"{self.base}/{_safe_name(name)}"

    def exists(self, name: str) -> bool:
        response = self.client.head(self._url(name))
        return response.status_code == 200

    def get(self, name: str) -> bytes:
        response = self.client.get(self._url(name))
        if response.status_code != 200:
            raise ConfigError(f"image store is missing {name}")
        return response.content

    def get_to(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with self.client.stream("GET", self._url(name)) as response:
            if response.status_code != 200:
                raise ConfigError(f"image store is missing {name}")
            with tmp.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        tmp.replace(dest)

    def put_bytes(self, name: str, data: bytes) -> None:
        del name, data
        raise ConfigError("https:// image stores are read-only")

    def put_file(self, name: str, source: Path) -> None:
        del source
        self.put_bytes(name, b"")


class ImageStore:
    def exists(self, name: str) -> bool:
        raise NotImplementedError

    def get(self, name: str) -> bytes:
        raise NotImplementedError

    def put_bytes(self, name: str, data: bytes) -> None:
        raise NotImplementedError

    def put_file(self, name: str, source: Path) -> None:
        raise NotImplementedError


def open_image_store(
    uri: str,
    settings: Settings | None = None,
    *,
    write: bool,
    client: object | None = None,
) -> FileImageStore | S3ImageStore | HttpImageStore:
    parsed = parse_image_uri(uri)
    if parsed.scheme == "https":
        if write:
            raise ConfigError(
                "https:// image stores are read-only; publish to s3:// or file://"
            )
        http = client if isinstance(client, httpx.Client) else None
        return HttpImageStore(parsed.path, client=http)
    if parsed.scheme == "file":
        return FileImageStore(Path(parsed.path))
    if settings is None:
        raise ConfigError("s3 image source needs gateway settings")
    return S3ImageStore(settings, parsed, client=client)
