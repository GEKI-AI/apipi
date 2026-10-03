import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

import httpx

from apipi.common.s3 import make_s3_client
from apipi.config import ConfigError, Settings

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


def _s3_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return None
    error = response.get("Error")
    if not isinstance(error, dict):
        return None
    code = error.get("Code")
    if isinstance(code, str) and code:
        return code
    return None


def _s3_missing(exc: BaseException) -> bool:
    return _s3_code(exc) in {"NoSuchKey", "404", "NotFound"}


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def image_s3_credentials() -> tuple[str | None, str | None, str | None]:
    access = _blank(os.environ.get("APIPI_IMAGE_S3_ACCESS_KEY_ID"))
    secret = _blank(os.environ.get("APIPI_IMAGE_S3_SECRET_ACCESS_KEY"))
    profile = _blank(os.environ.get("APIPI_IMAGE_S3_PROFILE"))
    if (access is None) != (secret is None):
        raise ConfigError(
            "APIPI_IMAGE_S3_ACCESS_KEY_ID and "
            "APIPI_IMAGE_S3_SECRET_ACCESS_KEY must both be set"
        )
    if profile and access:
        raise ConfigError(
            "set APIPI_IMAGE_S3_PROFILE or "
            "APIPI_IMAGE_S3_ACCESS_KEY_ID and "
            "APIPI_IMAGE_S3_SECRET_ACCESS_KEY, not both"
        )
    return access, secret, profile


def image_s3_settings(settings: Settings) -> Settings:
    endpoint = _blank(settings.image_s3_endpoint)
    if endpoint is None:
        endpoint = settings.s3_endpoint
    region = _blank(settings.image_s3_region)
    if region is None:
        region = settings.s3_region
    addressing = settings.image_s3_addressing
    if addressing is None:
        addressing = settings.s3_addressing
    return settings.model_copy(
        update={
            "s3_endpoint": endpoint,
            "s3_region": region,
            "s3_addressing": addressing,
        }
    )


def make_image_s3_client(settings: Settings) -> object:
    access, secret, profile = image_s3_credentials()
    return make_s3_client(
        image_s3_settings(settings),
        missing=S3_MISSING,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        profile_name=profile,
    )


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
            client = make_image_s3_client(settings)
        self.client: Any = client

    def _raise_s3(self, name: str, exc: BaseException) -> NoReturn:
        if _s3_code(exc) == "NoSuchBucket":
            raise ConfigError(f"bucket {self.bucket} does not exist") from exc
        if _s3_missing(exc):
            raise ConfigError(f"image store is missing {name}") from exc
        raise exc

    def key(self, name: str) -> str:
        safe = _safe_name(name)
        if not self.prefix:
            return safe
        return f"{self.prefix}/{safe}"

    def exists(self, name: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            if _s3_code(exc) == "NoSuchBucket":
                raise ConfigError(f"bucket {self.bucket} does not exist") from exc
            if _s3_missing(exc):
                return False
            raise
        return True

    def get(self, name: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            self._raise_s3(name, exc)
        body = response["Body"]
        data = body.read()
        if not isinstance(data, bytes):
            raise ConfigError(f"image store returned no bytes for {name}")
        return data

    def get_to(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        try:
            self._stream_to(name, tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(dest)

    def _stream_to(self, name: str, dest: Path) -> None:
        download = getattr(self.client, "download_file", None)
        if callable(download):
            try:
                download(self.bucket, self.key(name), str(dest))
            except Exception as exc:
                self._raise_s3(name, exc)
            return
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:
            self._raise_s3(name, exc)
        body = response["Body"]
        try:
            with dest.open("wb") as out:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise ConfigError(f"image store returned no bytes for {name}")
                    out.write(chunk)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def put_bytes(self, name: str, data: bytes) -> None:
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(name), Body=data)
        except Exception as exc:
            if _s3_code(exc) == "NoSuchBucket":
                raise ConfigError(f"bucket {self.bucket} does not exist") from exc
            raise

    def put_file(self, name: str, source: Path) -> None:
        upload = getattr(self.client, "upload_file", None)
        if callable(upload):
            try:
                upload(str(source), self.bucket, self.key(name))
            except Exception as exc:
                if _s3_code(exc) == "NoSuchBucket":
                    raise ConfigError(f"bucket {self.bucket} does not exist") from exc
                raise
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
        if response.status_code == 200:
            return True
        if response.status_code in {403, 405}:
            probe = self.client.get(self._url(name), headers={"Range": "bytes=0-0"})
            return probe.status_code in {200, 206}
        return False

    def get(self, name: str) -> bytes:
        response = self.client.get(self._url(name))
        if response.status_code != 200:
            raise ConfigError(f"image store is missing {name}")
        return response.content

    def get_to(self, name: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        headers: dict[str, str] = {}
        mode = "wb"
        start = tmp.stat().st_size if tmp.is_file() else 0
        if start:
            headers["Range"] = f"bytes={start}-"
            mode = "ab"
        with self.client.stream("GET", self._url(name), headers=headers) as response:
            if start and response.status_code == 200:
                tmp.unlink(missing_ok=True)
                mode = "wb"
            elif response.status_code not in {200, 206}:
                raise ConfigError(f"image store is missing {name}")
            with tmp.open(mode) as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        tmp.replace(dest)

    def put_bytes(self, name: str, data: bytes) -> None:
        del name, data
        raise ConfigError("https:// image stores are read-only")

    def put_file(self, name: str, source: Path) -> None:
        del source
        self.put_bytes(name, b"")


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
                "https:// image stores are read-only; push to s3:// or file://"
            )
        http = client if isinstance(client, httpx.Client) else None
        return HttpImageStore(parsed.path, client=http)
    if parsed.scheme == "file":
        return FileImageStore(Path(parsed.path))
    if settings is None:
        raise ConfigError("s3 image source needs gateway settings")
    return S3ImageStore(settings, parsed, client=client)
