"""Copy a previous official image store when the image inputs match.

Exit 10 when the images must be built. Exit 0 when the new store is written.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from apipi.config import ConfigError
from apipi.worker.pi.image_catalog import republish_store, version_prefix

REBUILD = 10


def _releases() -> list[dict[str, object]]:
    raw = subprocess.check_output(
        ["gh", "release", "list", "--limit", "30", "--json", "tagName,isDraft"],
        text=True,
    )
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ConfigError("could not list GitHub releases")
    return [item for item in parsed if isinstance(item, dict)]


def _asset_names(tag: str) -> set[str]:
    raw = subprocess.check_output(
        ["gh", "release", "view", tag, "--json", "assets", "--jq", ".assets[].name"],
        text=True,
    )
    return {line.strip() for line in raw.splitlines() if line.strip()}


def previous_store_tag(current: str) -> str | None:
    wanted = current.removeprefix("v")
    for item in _releases():
        tag = str(item.get("tagName", ""))
        if item.get("isDraft") or tag.removeprefix("v") == wanted:
            continue
        if "index.json" in _asset_names(tag):
            return tag
    return None


def download_store(tag: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "gh",
            "release",
            "download",
            tag,
            "--dir",
            str(dest),
            "--pattern",
            "index.json",
            "--pattern",
            "SHA256SUMS",
            "--pattern",
            "*.manifest.json",
            "--pattern",
            "*.ext4.zst",
            "--pattern",
            "*.ext4.zst.part-*",
            "--pattern",
            "vmlinux-*",
        ],
        check=True,
    )


def main() -> int:
    current = os.environ.get("GITHUB_REF_NAME", "").removeprefix("v")
    if len(sys.argv) >= 3 and sys.argv[1] == "--out":
        out = Path(sys.argv[2])
    else:
        print("usage: reuse_image_store.py --out DIR", file=sys.stderr)
        return 1
    if not current:
        print("GITHUB_REF_NAME is unset", file=sys.stderr)
        return 1
    tag = previous_store_tag(current)
    if tag is None:
        print("no previous image store to reuse")
        return REBUILD
    incoming = out.parent / ".previous-image-store"
    download_store(tag, incoming)
    files = {
        path.name: path.read_bytes() for path in incoming.iterdir() if path.is_file()
    }
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    try:
        published = republish_store(files, version=current, commit=commit)
    except ConfigError as exc:
        print(exc)
        return REBUILD
    dest = out / version_prefix(current)
    dest.mkdir(parents=True, exist_ok=True)
    for name, blob in published.items():
        (dest / name).write_bytes(blob)
    print(f"reused image store {tag} as {version_prefix(current)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
