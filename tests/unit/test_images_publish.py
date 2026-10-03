import io
import json
from pathlib import Path

import pytest

from apipi.cli import main
from apipi.common.images import load_index, manifest_name, sha256_file
from apipi.config import ConfigError, Settings
from apipi.worker.pi.image_ops import package_image, publish_images
from apipi.worker.pi.image_store import open_image_store


class _Missing(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        del bucket
        self.uploads.append(key)
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


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )


def _package(tmp_path: Path, image_id: str) -> Path:
    work = tmp_path / "src" / image_id
    work.mkdir(parents=True)
    rootfs = work / ("rootfs.ext4" if image_id == "default" else "rootfs-browser.ext4")
    kernel = work / "vmlinux"
    rootfs.write_bytes(f"rootfs-{image_id}".encode())
    kernel.write_bytes(b"kernel-bytes")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    package_image(
        image_id=image_id,
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
    )
    return out


def test_package_image_writes_digest(tmp_path: Path) -> None:
    out = _package(tmp_path, "default")
    manifests = [
        path for path in out.glob("*.json") if not path.name.startswith("vmlinux-")
    ]
    assert len(manifests) == 1
    text = manifests[0].read_text()
    assert '"schema": 2' in text
    from apipi.worker.pi.version import PINNED_PI

    assert f"{PINNED_PI}-" in text
    zst = next(out.glob("*.ext4.zst"))
    assert sha256_file(zst)


class _MissingBucket:
    def __init__(self) -> None:
        self.puts = 0

    def head_object(self, **kwargs: object) -> dict[str, object]:
        del kwargs
        raise _Missing("NoSuchBucket")

    def put_object(self, **kwargs: object) -> None:
        del kwargs
        self.puts += 1

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        del filename, bucket, key
        self.puts += 1


def _stamp(path: Path, created_at: str) -> None:
    data = json.loads(path.read_text())
    data["created_at"] = created_at
    path.write_text(json.dumps(data, indent=2) + "\n")


def test_publish_requires_store_version(tmp_path: Path) -> None:
    out = _package(tmp_path, "default")
    store = open_image_store((tmp_path / "store").as_uri(), _settings(), write=True)
    with pytest.raises(ConfigError, match="push requires --store-version"):
        publish_images(store, out, ids=["default"])


def test_publish_versioned_writes_index(tmp_path: Path) -> None:
    out = _package(tmp_path, "default")
    _package(tmp_path, "browser")
    store_dir = tmp_path / "store" / "0.14.0"
    store = open_image_store(store_dir.as_uri(), _settings(), write=True)
    planned = publish_images(
        store, out, ids=["default"], store_version="0.14.0", dry_run=True
    )
    assert "index.json" in planned
    planned = publish_images(store, out, ids=["default"], store_version="0.14.0")
    assert "index.json" in planned
    index = load_index((store_dir / "index.json").read_text())
    assert [item.id for item in index.images] == ["default"]
    assert index.schema_version == 2


def test_publish_dry_run_writes_nothing(tmp_path: Path) -> None:
    out = _package(tmp_path, "default")
    store_dir = tmp_path / "dry" / "0.14.0"
    store = open_image_store(store_dir.as_uri(), _settings(), write=True)
    planned = publish_images(
        store, out, ids=["default"], store_version="0.14.0", dry_run=True
    )
    assert "index.json" in planned
    assert not store_dir.exists() or not any(store_dir.iterdir())


def test_publish_s3_uses_prefix(tmp_path: Path) -> None:
    out = _package(tmp_path, "browser")
    fake = FakeS3()
    store = open_image_store(
        "s3://images/apipi/0.14.0",
        _settings(),
        write=True,
        client=fake,
    )
    publish_images(store, out, ids=["browser"], store_version="0.14.0")
    assert any(key.startswith("apipi/0.14.0/") for key in fake.objects)
    assert "apipi/0.14.0/index.json" in fake.objects


def test_publish_keeps_newest_build(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    work = tmp_path / "src" / "default"
    work.mkdir(parents=True)
    rootfs = work / "rootfs.ext4"
    kernel = work / "vmlinux"
    rootfs.write_bytes(b"rootfs-default")
    kernel.write_bytes(b"kernel-bytes")
    older = package_image(
        image_id="default",
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
        pi_version="9.9.9",
    )
    newer = package_image(
        image_id="default",
        rootfs=rootfs,
        kernel=kernel,
        out_dir=out,
        arch="x86_64",
        pi_version="0.1.0",
    )
    _stamp(
        out / manifest_name(older.id, older.version, older.arch),
        "2020-01-01T00:00:00Z",
    )
    _stamp(
        out / manifest_name(newer.id, newer.version, newer.arch),
        "2024-06-01T00:00:00Z",
    )
    store_dir = tmp_path / "store" / "0.14.0"
    publish_images(
        open_image_store(store_dir.as_uri(), write=True),
        out,
        ids=["default"],
        store_version="0.14.0",
    )
    index = load_index((store_dir / "index.json").read_text())
    assert [item.version for item in index.images] == [newer.version]


def test_publish_missing_bucket(tmp_path: Path) -> None:
    out = _package(tmp_path, "default")
    fake = _MissingBucket()
    store = open_image_store(
        "s3://missing/apipi/0.14.0",
        _settings(),
        write=True,
        client=fake,
    )
    with pytest.raises(ConfigError, match="bucket missing does not exist"):
        publish_images(store, out, ids=["default"], store_version="0.14.0")
    assert fake.puts == 0


def test_https_publish_rejected() -> None:
    with pytest.raises(ConfigError, match="read-only"):
        open_image_store("https://example.com/images", _settings(), write=True)


def test_cli_push_dry_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _package(tmp_path, "default")
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    assert (
        main(
            [
                "images",
                "push",
                "--to",
                (tmp_path / "cli-store").as_uri(),
                "--from",
                str(out),
                "--store-version",
                "0.14.0",
                "--dry-run",
            ]
        )
        == 0
    )
    assert "index.json" in capsys.readouterr().out


def test_cli_push_defaults_to_image_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _package(tmp_path, "default")
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_IMAGE_SOURCE", (tmp_path / "from-env").as_uri())
    assert (
        main(
            [
                "images",
                "push",
                "--from",
                str(out),
                "--store-version",
                "0.14.0",
                "--dry-run",
            ]
        )
        == 0
    )
    assert "index.json" in capsys.readouterr().out


def test_cli_push_requires_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _package(tmp_path, "default")
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.delenv("APIPI_IMAGE_SOURCE", raising=False)
    assert main(["images", "push", "--from", str(out)]) == 1
    assert "APIPI_IMAGE_SOURCE" in capsys.readouterr().err


def test_cli_push_rejects_https(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _package(tmp_path, "default")
    monkeypatch.setenv("DATABASE_URL", "postgresql://apipi:apipi@localhost:5432/apipi")
    monkeypatch.setenv("APIPI_IMAGE_SOURCE", "https://example.com/images")
    assert main(["images", "push", "--from", str(out)]) == 1
    assert "read-only" in capsys.readouterr().err
