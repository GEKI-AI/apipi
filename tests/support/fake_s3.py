import hashlib
import io
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from botocore.exceptions import ClientError


class FakeS3Error(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.presigns: list[dict[str, Any]] = []
        self.ranges: list[str] = []
        self.downloads: list[str] = []
        self.gets: list[str] = []

    def put_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(key, str)
        assert isinstance(body, bytes)
        self.objects[key] = body
        ctype = kwargs.get("ContentType")
        if isinstance(ctype, str):
            self.types[key] = ctype

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        self.gets.append(key)
        if key not in self.objects:
            raise FakeS3Error("NoSuchKey")
        data = self.objects[key]
        ranged = kwargs.get("Range")
        if isinstance(ranged, str):
            self.ranges.append(ranged)
            first, last = ranged.removeprefix("bytes=").split("-")
            data = data[int(first) : int(last) + 1]
        return {"Body": io.BytesIO(data)}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key not in self.objects:
            raise FakeS3Error("404")
        return {
            "ContentLength": len(self.objects[key]),
            "ContentType": self.types.get(key, "application/octet-stream"),
            "ETag": self._etag(key),
        }

    def _etag(self, key: str) -> str:
        return f'"{hashlib.md5(self.objects[key]).hexdigest()}"'

    def copy_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        source = kwargs["CopySource"]
        assert isinstance(key, str)
        assert isinstance(source, dict)
        source_key = source["Key"]
        if source_key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "CopyObject")
        match = kwargs.get("CopySourceIfMatch")
        if match is not None and match != self._etag(source_key):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "CopyObject")
        self.objects[key] = self.objects[source_key]
        if source_key in self.types:
            self.types[key] = self.types[source_key]

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        del bucket
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        del bucket
        self.downloads.append(key)
        if key not in self.objects:
            raise FakeS3Error("NoSuchKey")
        Path(filename).write_bytes(self.objects[key])

    def put_url(self, url: str, body: bytes, content_type: str | None = None) -> int:
        """A client PUT to a presigned URL: 403 when a signed header differs."""
        key = urlsplit(url).path.lstrip("/")
        signed = next(
            params
            for params in reversed(self.presigns)
            if params["Key"] == key and "ContentLength" in params
        )
        if signed["ContentLength"] != len(body):
            return 403
        if signed.get("ContentType") not in (None, content_type):
            return 403
        self.put_object(Key=key, Body=body, ContentType=content_type)
        return 200

    def generate_presigned_url(
        self,
        ClientMethod: str,
        Params: dict[str, Any],
        ExpiresIn: int = 900,
        HttpMethod: str | None = None,
    ) -> str:
        del ClientMethod, ExpiresIn
        self.presigns.append(dict(Params))
        key = Params["Key"]
        method = HttpMethod or "GET"
        query = {"presign": "1", "method": method}
        disposition = Params.get("ResponseContentDisposition")
        if isinstance(disposition, str):
            query["response-content-disposition"] = disposition
        response_type = Params.get("ResponseContentType")
        if isinstance(response_type, str):
            query["response-content-type"] = response_type
        return f"https://bucket.example/{key}?{urlencode(query)}"

    def delete_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        if isinstance(key, str):
            self.objects.pop(key, None)

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = kwargs.get("Prefix", "")
        assert isinstance(prefix, str)
        contents = [
            {"Key": key, "Size": len(data)}
            for key, data in self.objects.items()
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}
