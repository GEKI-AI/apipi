import io
from pathlib import Path

import httpx
import pytest

from apipi.config import ConfigError, Settings
from apipi.worker.pi.image_ops import package_image, publish_images
from apipi.worker.pi.image_pull import list_images, pull_images
from apipi.worker.pi.image_store import open_image_store
from apipi.worker.pi.images import read_current
from apipi.worker.pi.microvm import microvm_images


class _Missing(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        del bucket
        self.objects[key] = Path(filename).read_bytes()

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
    store = tmp_path / "store"
    publish_images(open_image_store(store.as_uri(), write=True), out, ids=["default"])
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
    _store, payload = _published(tmp_path)
    fake = FakeS3()
    out = tmp_path / "out"
    s3 = open_image_store(
        "s3://images/apipi", _settings(tmp_path), write=True, client=fake
    )
    publish_images(s3, out, ids=["default"])
    settings = _settings(tmp_path, image_source="s3://images/apipi")
    pull_images(settings, ids=["default"], client=fake)
    version = read_current(tmp_path / "images", "default")
    assert version is not None
    rootfs = tmp_path / "images" / "default" / version / "rootfs.ext4"
    assert rootfs.read_bytes() == payload


def test_pull_https(tmp_path: Path) -> None:
    store, payload = _published(tmp_path)
    files = {path.name: path.read_bytes() for path in store.iterdir() if path.is_file()}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name not in files:
            return httpx.Response(404)
        return httpx.Response(200, content=files[name])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings(tmp_path, image_source="https://images.example/apipi")
    pull_images(settings, ids=["default"], client=client)
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


def test_images_dir_beats_legacy_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store, payload = _published(tmp_path)
    settings = _settings(tmp_path, image_source=store.as_uri())
    pull_images(settings, ids=["default"])
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = tmp_path / "cache" / "apipi" / "microvm"
    cache.mkdir(parents=True)
    (cache / "vmlinux").write_bytes(b"old-kernel")
    (cache / "rootfs.ext4").write_bytes(b"old-rootfs")
    _kernel, rootfs = microvm_images(settings, image="default")
    assert Path(rootfs).read_bytes() == payload
