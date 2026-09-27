import io
import json
import zipfile

import pytest

from apipi.gateway.errors import ApiError
from apipi.services.bundles import read_bundle


def _zip(entries: dict[str, bytes | str], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, payload in entries.items():
            data = payload if isinstance(payload, bytes) else payload.encode()
            archive.writestr(name, data)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, b"target")
    return buf.getvalue()


def _manifest(**extra: object) -> str:
    body = {
        "schema_version": "1.0",
        "kind": "apipi.agent",
        "agent": {"name": "bot", "model": "test"},
    }
    body.update(extra)
    return json.dumps(body)


def test_read_bundle_accepts_agent_json() -> None:
    parsed = read_bundle(_zip({"agent.json": _manifest()}), max_bytes=10000)
    assert parsed["manifest"]["agent"]["name"] == "bot"


def test_rejects_unsafe_archives() -> None:
    cases = [
        _zip({"../secret": "x"}),
        _zip({"/etc/passwd": "x"}),
        _zip({"notes.txt": "nope"}),
        _zip({"agent.json": _manifest()}, symlink="link"),
        b"not-a-zip",
        _zip({"README.md": "hi"}),
        _zip(
            {"agent.json": json.dumps({"schema_version": "2.0", "kind": "apipi.agent"})}
        ),
    ]
    for blob in cases:
        with pytest.raises(ApiError):
            read_bundle(blob, max_bytes=10000)


def test_newer_major_has_version_code() -> None:
    blob = _zip(
        {
            "agent.json": json.dumps(
                {"schema_version": "2.0", "kind": "apipi.agent", "agent": {}}
            )
        }
    )
    with pytest.raises(ApiError) as raised:
        read_bundle(blob, max_bytes=10000)
    assert raised.value.code == "bundle_version_unsupported"


def test_zip_bomb_ratio() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("agent.json", "0" * 20000)
    with pytest.raises(ApiError, match="ratio"):
        read_bundle(buf.getvalue(), max_bytes=100000)
