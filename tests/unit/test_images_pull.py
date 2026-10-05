import io
from pathlib import Path

import httpx
import pytest

from apipi.common.images import read_current
from apipi.config import ConfigError, Settings, load_settings
from apipi.worker.pi.image_ops import package_image, publish_images
from apipi.worker.pi.image_pull import list_images, pull_images
from apipi.worker.pi.image_store import S3ImageStore, open_image_store, parse_image_uri
from apipi.worker.pi.microvm import microvm_images


class _Missing(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.gets: list[str] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        del bucket
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        del bucket
        self.downloads.append(key)
        if key not in self.objects:
            raise _Missing("NoSuchKey")
        Path(filename).write_bytes(self.objects[key])

    def put_object(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(key, str)
        assert isinstance(body, bytes)
        self.objects[key] = body

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        if key not in self.objects:
            raise _Missing("404")
        return {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, str)
        self.gets.append(key)
        if key not in self.objects:
            raise _Missing("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[key])}


def _settings(
    tmp_path: Path,
    *,
    image_source: str | None = None,
    sandbox_images: list[str] | None = None,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        images_dir=str(tmp_path / "images"),
        image_source=image_source,
        sandbox_images=sandbox_images,
    )


def _published(tmp_path: Path) -> tuple[Path, bytes]:
    from apipi import __version__
    from apipi.worker.pi.image_catalog import version_prefix

    version = version_prefix(__version__)
    work = tmp_path / "src"
    work.mkdir()
    rootfs = work / "rootfs.ext4"
    kernel = work / "vmlinux"
    payload = b"rootfs-bytes"
    rootfs.write_bytes(payload)
    kernel.write_bytes(b"kernel-bytes")
    out = tmp_path / "out"
    out.mkdir()
    package_image(
        image_id="default",
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
    )
    store = tmp_path / "store" / version
    publish_images(
        open_image_store(store.as_uri(), write=True),
        out,
        ids=["default"],
        store_version=version,
    )
    return store, payload


def test_pull_file_verifies_and_installs(tmp_path: Path) -> None:
    store, payload = _published(tmp_path)
    settings = _settings(tmp_path, image_source=store.as_uri())
    lines = pull_images(settings, ids=["default"])
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    assert lines == [f"default {version}"]
    installed = tmp_path / "images" / "default" / version
    assert (installed / "rootfs.ext4").read_bytes() == payload
    assert not (tmp_path / "images" / ".incoming").exists() or not any(
        (tmp_path / "images" / ".incoming").iterdir()
    )


def test_pull_corrupt_leaves_no_partial(tmp_path: Path) -> None:
    store, _payload = _published(tmp_path)
    zst = next(store.glob("*.ext4.zst"))
    zst.write_bytes(b"not-a-zstd-image")
    settings = _settings(tmp_path, image_source=store.as_uri())
    with pytest.raises(ConfigError, match="sha256 mismatch"):
        pull_images(settings, ids=["default"])
    assert read_current(tmp_path / "images", "default") is None
    assert not (tmp_path / "images" / "default").exists()


def test_pull_s3(tmp_path: Path) -> None:
    from apipi import __version__ as _v
    from apipi.worker.pi.image_catalog import version_prefix

    _store, payload = _published(tmp_path)
    fake = FakeS3()
    out = tmp_path / "out"
    versioned = f"s3://images/apipi/{version_prefix(_v)}"
    s3 = open_image_store(versioned, _settings(tmp_path), write=True, client=fake)
    publish_images(s3, out, ids=["default"], store_version=version_prefix(_v))
    settings = _settings(tmp_path, image_source=versioned)
    pull_images(settings, ids=["default"], client=fake)
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    rootfs = tmp_path / "images" / "default" / version / "rootfs.ext4"
    assert rootfs.read_bytes() == payload


def test_pull_https(tmp_path: Path) -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    store, payload = _published(tmp_path)
    files = {path.name: path.read_bytes() for path in store.iterdir() if path.is_file()}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        settings = _settings(tmp_path, image_source="https://images.example/apipi")
        pull_images(settings, ids=["default"], client=client)
    finally:
        client.close()
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    rootfs = tmp_path / "images" / "default" / version / "rootfs.ext4"
    assert rootfs.read_bytes() == payload


def test_pull_unknown_and_images_setting(tmp_path: Path) -> None:
    store, _payload = _published(tmp_path)
    settings = _settings(
        tmp_path, image_source=store.as_uri(), sandbox_images=["default"]
    )
    assert pull_images(settings)[0].startswith("default ")
    with pytest.raises(ConfigError, match="unknown image nope"):
        pull_images(settings, ids=["nope"])


def test_list_local_and_remote(tmp_path: Path) -> None:
    store, _payload = _published(tmp_path)
    settings = _settings(tmp_path, image_source=store.as_uri())
    pull_images(settings, ids=["default"])
    rows = list_images(settings, remote=True)
    assert rows[0][0] == "default"
    assert rows[0][3] == "local+remote"


def _published_versioned(tmp_path: Path) -> Path:
    work = tmp_path / "vsrc"
    work.mkdir()
    rootfs = work / "rootfs.ext4"
    kernel = work / "vmlinux"
    rootfs.write_bytes(b"rootfs-bytes")
    kernel.write_bytes(b"kernel-bytes")
    out = tmp_path / "vout"
    out.mkdir()
    package_image(
        image_id="default",
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
    )
    versioned = tmp_path / "vstore" / "v0.12.1"
    publish_images(
        open_image_store(versioned.as_uri(), write=True),
        out,
        ids=["default"],
        store_version="0.12.1",
    )
    return versioned


def test_pull_from_versioned_store_without_latest_flag(tmp_path: Path) -> None:
    versioned = _published_versioned(tmp_path)
    settings = _settings(tmp_path, image_source=versioned.as_uri())
    lines = pull_images(settings, ids=["default"])
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    assert lines == [f"default {version}"]


def test_list_remote_from_versioned_store(tmp_path: Path) -> None:
    versioned = _published_versioned(tmp_path)
    settings = _settings(tmp_path, image_source=versioned.as_uri())
    rows = list_images(settings, remote=True)
    assert [(row[0], row[3]) for row in rows] == [("default", "remote")]


def test_microvm_images_resolve_the_pulled_image(tmp_path: Path) -> None:
    store, payload = _published(tmp_path)
    settings = _settings(tmp_path, image_source=store.as_uri())
    pull_images(settings, ids=["default"])
    _kernel, rootfs = microvm_images(settings, image="default")
    assert Path(rootfs).read_bytes() == payload


def test_pull_s3_uses_get_to_for_blobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from apipi import __version__ as _v
    from apipi.worker.pi.image_catalog import version_prefix

    _store, payload = _published(tmp_path)
    fake = FakeS3()
    out = tmp_path / "out"
    versioned = f"s3://images/apipi/{version_prefix(_v)}"
    s3 = open_image_store(versioned, _settings(tmp_path), write=True, client=fake)
    publish_images(s3, out, ids=["default"], store_version=version_prefix(_v))
    original = S3ImageStore.get

    def guarded(self: S3ImageStore, name: str) -> bytes:
        if name.endswith(".zst"):
            raise AssertionError(name)
        return original(self, name)

    monkeypatch.setattr(S3ImageStore, "get", guarded)
    settings = _settings(tmp_path, image_source=versioned)
    pull_images(settings, ids=["default"], client=fake)
    assert any(key.endswith(".ext4.zst") for key in fake.downloads)
    assert any(key.endswith("vmlinux-x86_64.zst") for key in fake.downloads)
    assert not any(key.endswith(".zst") for key in fake.gets)
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    rootfs = tmp_path / "images" / "default" / version / "rootfs.ext4"
    assert rootfs.read_bytes() == payload


def test_s3_get_to_streams_body(tmp_path: Path) -> None:
    payload = b"z" * (1024 * 1024 + 8)

    class Body:
        def __init__(self) -> None:
            self.data = payload
            self.sizes: list[int] = []

        def read(self, n: int = -1) -> bytes:
            self.sizes.append(n)
            if n < 0:
                chunk = self.data
                self.data = b""
                return chunk
            chunk = self.data[:n]
            self.data = self.data[n:]
            return chunk

    body = Body()

    class Client:
        def get_object(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            return {"Body": body}

    store = S3ImageStore(
        _settings(tmp_path),
        parse_image_uri("s3://images/apipi"),
        client=Client(),
    )
    dest = tmp_path / "blob.zst"
    store.get_to("blob.zst", dest)
    assert dest.read_bytes() == payload
    assert body.sizes
    assert -1 not in body.sizes


def test_image_s3_settings_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_client(
        settings: Settings,
        *,
        missing: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        profile_name: str | None = None,
    ) -> object:
        del missing
        seen["endpoint"] = settings.s3_endpoint
        seen["region"] = settings.s3_region
        seen["addressing"] = settings.s3_addressing
        seen["access"] = aws_access_key_id
        seen["secret"] = aws_secret_access_key
        seen["profile"] = profile_name
        return object()

    monkeypatch.setattr("apipi.worker.pi.image_store.make_s3_client", fake_client)
    monkeypatch.delenv("APIPI_IMAGE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_PROFILE", raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        s3_endpoint="https://artifacts.example",
        s3_region="hel1",
        s3_addressing="path",
    )
    open_image_store("s3://images/apipi", settings, write=False)
    assert seen["endpoint"] == "https://artifacts.example"
    assert seen["region"] == "hel1"
    assert seen["addressing"] == "path"
    assert seen["access"] is None
    assert seen["profile"] is None
    assert settings.s3_endpoint == "https://artifacts.example"

    monkeypatch.setenv("APIPI_IMAGE_S3_ACCESS_KEY_ID", "AKI")
    monkeypatch.setenv("APIPI_IMAGE_S3_SECRET_ACCESS_KEY", "secret")
    overridden = settings.model_copy(
        update={
            "image_s3_endpoint": "https://images.example",
            "image_s3_region": "fsn1",
            "image_s3_addressing": "virtual",
        }
    )
    open_image_store("s3://images/apipi", overridden, write=False)
    assert seen["endpoint"] == "https://images.example"
    assert seen["region"] == "fsn1"
    assert seen["addressing"] == "virtual"
    assert seen["access"] == "AKI"
    assert seen["secret"] == "secret"
    assert overridden.s3_endpoint == "https://artifacts.example"

    monkeypatch.delenv("APIPI_IMAGE_S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("APIPI_IMAGE_S3_PROFILE", "images")
    open_image_store("s3://images/apipi", settings, write=False)
    assert seen["profile"] == "images"
    assert seen["access"] is None

    monkeypatch.setenv("APIPI_IMAGE_S3_ACCESS_KEY_ID", "AKI")
    monkeypatch.setenv("APIPI_IMAGE_S3_SECRET_ACCESS_KEY", "secret")
    with pytest.raises(ConfigError, match="not both"):
        open_image_store("s3://images/apipi", settings, write=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_PROFILE", raising=False)
    with pytest.raises(ConfigError, match="must both be set"):
        open_image_store("s3://images/apipi", settings, write=False)


def test_image_s3_settings_from_toml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_S3_ENDPOINT", "https://artifacts.example")
    monkeypatch.setenv("APIPI_S3_REGION", "hel1")
    monkeypatch.setenv("APIPI_S3_ADDRESSING", "path")
    monkeypatch.delenv("APIPI_IMAGE_S3_ENDPOINT", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_REGION", raising=False)
    monkeypatch.delenv("APIPI_IMAGE_S3_ADDRESSING", raising=False)
    (tmp_path / "apipi.toml").write_text(
        "[sandbox]\n"
        'image_s3_endpoint = "https://images.example"\n'
        'image_s3_region = "fsn1"\n'
        'image_s3_addressing = "virtual"\n'
    )
    loaded = load_settings()
    assert loaded.image_s3_endpoint == "https://images.example"
    assert loaded.image_s3_region == "fsn1"
    assert loaded.image_s3_addressing == "virtual"
    assert loaded.s3_endpoint == "https://artifacts.example"
    assert loaded.s3_region == "hel1"
    assert loaded.s3_addressing == "path"
    monkeypatch.setenv("APIPI_IMAGE_S3_ENDPOINT", "https://from-env.example")
    assert load_settings().image_s3_endpoint == "https://from-env.example"
    (tmp_path / "apipi.toml").write_text('[sandbox]\nimage_s3_access_key_id = "AKI"\n')
    with pytest.raises(ConfigError, match="unknown setting"):
        load_settings()
