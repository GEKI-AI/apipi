"""Compare image download pins with upstream checksum files.

Run before an image publish. Unit tests do not call the network.
"""

import sys
import urllib.request

from apipi.worker.pi.version import (
    PINNED_AGENT_BROWSER,
    PINNED_AGENT_BROWSER_SHA256_X86_64,
    PINNED_CHROME_HEADLESS_SHELL,
    PINNED_CHROME_HEADLESS_SHELL_SHA256_X86_64,
    PINNED_NODE,
    PINNED_NODE_SHA256_AARCH64,
    PINNED_NODE_SHA256_X86_64,
    PINNED_UV,
    PINNED_UV_SHA256_AARCH64,
    PINNED_UV_SHA256_X86_64,
)

NODE_NAMES = {
    f"node-{PINNED_NODE}-linux-x64.tar.xz": PINNED_NODE_SHA256_X86_64,
    f"node-{PINNED_NODE}-linux-arm64.tar.xz": PINNED_NODE_SHA256_AARCH64,
}
UV_NAMES = {
    "uv-x86_64-unknown-linux-musl.tar.gz": PINNED_UV_SHA256_X86_64,
    "uv-aarch64-unknown-linux-musl.tar.gz": PINNED_UV_SHA256_AARCH64,
}


def parse_sums(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest, _, name = stripped.partition(" ")
        found[name.strip()] = digest
    return found


def require_digest(sums: dict[str, str], name: str, expected: str, source: str) -> None:
    actual = sums.get(name)
    if actual is None:
        raise SystemExit(f"{source} has no line for {name}")
    if actual != expected:
        raise SystemExit(f"{name} pin {expected} does not match {source} {actual}")


def fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode()


def fetch_optional(url: str) -> str:
    try:
        return fetch(url)
    except Exception as exc:
        print(f"skip {url}: {exc}", file=sys.stderr)
        return ""


def main() -> None:
    node = parse_sums(fetch(f"https://nodejs.org/dist/{PINNED_NODE}/SHASUMS256.txt"))
    for name, expected in NODE_NAMES.items():
        require_digest(node, name, expected, "nodejs.org")
    uv = parse_sums(
        fetch_optional(
            f"https://github.com/astral-sh/uv/releases/download/{PINNED_UV}/sha256.sum"
        )
    )
    for name, expected in UV_NAMES.items():
        if name in uv:
            require_digest(uv, name, expected, "uv")
    browser = fetch_optional(
        "https://github.com/vercel-labs/agent-browser/releases/download/"
        f"v{PINNED_AGENT_BROWSER}/agent-browser-linux-x64.sha256"
    ).split()
    if browser and browser[0] != PINNED_AGENT_BROWSER_SHA256_X86_64:
        raise SystemExit("agent-browser pin does not match the release checksum")
    chrome_name = "chrome-headless-shell-linux64.zip"
    chrome = fetch_optional(
        "https://storage.googleapis.com/chrome-for-testing-public/"
        f"{PINNED_CHROME_HEADLESS_SHELL}/linux64/{chrome_name}.sha256"
    )
    if chrome:
        digest = parse_sums(chrome).get(chrome_name, chrome.split()[0])
        if digest != PINNED_CHROME_HEADLESS_SHELL_SHA256_X86_64:
            raise SystemExit("chrome-headless-shell pin does not match upstream")
    print("image download pins match upstream")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"image pin check failed: {exc}", file=sys.stderr)
        raise
