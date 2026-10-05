import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.support.git_server import BACKEND, SECRET, GitServer, bare_repo
from tests.support.microvm import image_paths, microvm_or_skip

from apipi.config import Settings
from apipi.env.setup import NetworkPolicy, write_network_policy
from apipi.protocol import ContextEnvCredential
from apipi.worker.egress import WorkerCA
from apipi.worker.egress.policy import PLACEHOLDER_NET
from apipi.worker.pi.microvm import spawn_microvm_pi
from apipi.worker.pi.proc import PiProc

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.microvm,
    pytest.mark.slow,
    pytest.mark.timeout(900),
]

SPLICED = os.environ.get("APIPI_E2E_EGRESS_HOST", "example.com")
OTHER = os.environ.get("APIPI_E2E_EGRESS_OTHER_HOST", "example.org")
INTERCEPTED = os.environ.get("APIPI_E2E_EGRESS_INTERCEPT_HOST", "github.com")
GIT_REPO = os.environ.get(
    "APIPI_E2E_EGRESS_GIT_REPO", "https://github.com/octocat/Hello-World.git"
)
PRIVATE = os.environ.get("APIPI_E2E_EGRESS_PRIVATE_HOST", "localtest.me")
PROBE = "/workspace/egress_probe.py"

PROBE_SOURCE = r"""
import json
import socket
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

config = json.loads(Path("/workspace/egress_probe.json").read_text())
net = dict(
    line.split("=", 1)
    for line in Path("/workspace/.apipi/net").read_text().split()
    if "=" in line
)
gateway = net.get("GUEST_GW", "").strip("'")
results = {}


def run(name, argv, timeout=40):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        results[name] = {
            "rc": done.returncode,
            "out": done.stdout[-300:],
            "err": done.stderr[-300:],
        }
    except Exception as exc:
        results[name] = {"rc": -1, "out": "", "err": repr(exc)}


def check(name, func):
    try:
        results[name] = {"rc": 0, "out": str(func())[:300], "err": ""}
    except Exception as exc:
        results[name] = {"rc": 1, "out": "", "err": repr(exc)[:300]}


def tcp(host, port):
    with socket.create_connection((host, port), timeout=8):
        return "connected"


def issuer(host):
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            names = dict(item[0] for item in tls.getpeercert()["issuer"])
            return names.get("commonName", "")


def requests_get(url):
    try:
        import requests
    except ImportError:
        with urllib.request.urlopen(url, timeout=20) as reply:
            return f"urllib {reply.status}"
    return f"requests {requests.get(url, timeout=20).status_code}"


spliced = config["spliced"]
other = config["other"]
intercepted = config["intercepted"]
curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20"]
run("curl_spliced", [*curl, f"https://{spliced}/"])
run("curl_other", [*curl, f"https://{other}/"])
run("curl_ip_literal", [*curl, "https://1.1.1.1/"])
run(
    "curl_resolve_other_ip",
    [*curl, "-k", "--resolve", f"{spliced}:443:203.0.113.10", f"https://{spliced}/"],
)
check("tcp_22", lambda: tcp(spliced, 22))
check("dns_allowed", lambda: socket.getaddrinfo(spliced, 443)[0][4][0])
check("dns_other", lambda: socket.getaddrinfo(other, 443)[0][4][0])
run("curl_intercepted", [*curl, f"https://{intercepted}/"])
check("issuer_intercepted", lambda: issuer(intercepted))
check("issuer_spliced", lambda: issuer(spliced))
check("requests_intercepted", lambda: requests_get(f"https://{intercepted}/"))
run(
    "node_fetch_intercepted",
    [
        "node",
        "-e",
        f"fetch('https://{intercepted}/').then(r => console.log(r.status))"
        ".catch(e => { console.error(String(e)); process.exit(1) })",
    ],
)
run("git_ls_remote", ["git", "ls-remote", config["git_repo"], "HEAD"])
if config.get("private"):
    private = config["private"]
    check("dns_private", lambda: socket.getaddrinfo(private, 443)[0][4][0])
    run(
        "curl_private",
        [*curl, "--cacert", "/workspace/private-ca.pem", f"https://{private}/"],
    )
    check("tcp_host_direct", lambda: tcp(gateway, config["private_direct_port"]))
print(json.dumps({"type": "egress_probe", "results": results}), flush=True)
sys.stdin.read()
"""

ENABLED_PROBE_SOURCE = r"""
import json
import subprocess
import sys

results = {}
curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20"]
for name, argv in {
    "curl_public": [*curl, "https://example.com/"],
    "curl_ip_literal": [*curl, "https://1.1.1.1/"],
    "curl_metadata": [*curl, "http://169.254.169.254/"],
    "curl_private_resolve": [
        *curl,
        "-k",
        "--resolve",
        "example.com:443:10.0.0.1",
        "https://example.com/",
    ],
}.items():
    done = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    results[name] = {"rc": done.returncode, "out": done.stdout, "err": done.stderr}
print(json.dumps({"type": "egress_probe", "results": results}), flush=True)
sys.stdin.read()
"""


def _settings(tmp_path: Path, **extra: Any) -> Settings:
    kernel, rootfs = image_paths()
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        idle_ttl=timedelta(seconds=30),
        pi_command=f"python3 {PROBE}",
        sessions_dir=str(tmp_path / "sessions"),
        microvm_kernel=kernel,
        microvm_rootfs=rootfs,
        **extra,
    )
    microvm_or_skip(settings)
    return settings


async def _probe(proc: PiProc) -> dict[str, dict[str, Any]]:
    async def first() -> dict[str, Any]:
        async for event in proc._raw_events():
            if event.get("type") == "egress_probe":
                return event
        raise AssertionError("guest probe printed nothing")

    event = await asyncio.wait_for(first(), timeout=600)
    return event["results"]


class _PrivateUpstream:
    def __init__(self, ca: WorkerCA) -> None:
        self.ca = ca
        self.server: asyncio.Server | None = None
        self.direct: socket.socket | None = None
        self.direct_port = 0

    async def start(self) -> None:
        async def serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            with contextlib.suppress(Exception):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n"
                    b"Connection: close\r\n\r\nprivate"
                )
                await writer.drain()
            writer.close()

        context = self.ca.server_context(PRIVATE)
        try:
            self.server = await asyncio.start_server(
                serve, "127.0.0.1", 443, ssl=context
            )
        except OSError as exc:
            pytest.skip(f"cannot bind 127.0.0.1:443 for the private upstream: {exc}")
        self.direct = socket.create_server(("0.0.0.0", 0))
        self.direct_port = int(self.direct.getsockname()[1])

    def close(self) -> None:
        if self.server is not None:
            self.server.close()
        if self.direct is not None:
            self.direct.close()


@pytest.fixture
async def private_upstream(tmp_path: Path) -> AsyncIterator[_PrivateUpstream]:
    _settings(tmp_path)
    upstream = _PrivateUpstream(WorkerCA())
    await upstream.start()
    yield upstream
    upstream.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    yield cwd


def _failed(results: dict[str, dict[str, Any]], name: str) -> bool:
    return results[name]["rc"] != 0


async def test_restricted_guest_egress(
    tmp_path: Path, workspace: Path, private_upstream: _PrivateUpstream
) -> None:
    ca_file = tmp_path / "private-ca.pem"
    ca_file.write_bytes(private_upstream.ca.cert_pem)
    settings = _settings(
        tmp_path,
        microvm_egress_private_hosts=PRIVATE,
        microvm_egress_upstream_ca=str(ca_file),
    )
    (workspace / "egress_probe.py").write_text(PROBE_SOURCE)
    (workspace / "private-ca.pem").write_bytes(private_upstream.ca.cert_pem)
    (workspace / "egress_probe.json").write_text(
        json.dumps(
            {
                "spliced": SPLICED,
                "other": OTHER,
                "intercepted": INTERCEPTED,
                "git_repo": GIT_REPO,
                "private": PRIVATE,
                "private_direct_port": private_upstream.direct_port,
            }
        )
    )
    write_network_policy(
        workspace,
        NetworkPolicy(
            access="restricted", allowed_domains=(SPLICED, INTERCEPTED, PRIVATE)
        ),
    )
    proc = await spawn_microvm_pi(
        settings, cwd=str(workspace), tools=True, intercept_hosts=(INTERCEPTED,)
    )
    try:
        results = await _probe(proc)
    finally:
        await proc.terminate()
    report = json.dumps(results, indent=2)
    assert results["curl_spliced"]["rc"] == 0, report
    assert _failed(results, "curl_other"), report
    assert _failed(results, "curl_ip_literal"), report
    assert results["curl_resolve_other_ip"]["rc"] == 0, report
    assert _failed(results, "tcp_22"), report
    assert results["dns_allowed"]["rc"] == 0, report
    assert _failed(results, "dns_other"), report
    assert "Name or service not known" in results["dns_other"]["err"] or (
        "-2" in results["dns_other"]["err"]
    ), report
    assert results["issuer_spliced"]["out"] != "ApiPi worker egress CA", report
    assert results["issuer_intercepted"]["out"] == "ApiPi worker egress CA", report
    assert results["curl_intercepted"]["rc"] == 0, report
    assert results["requests_intercepted"]["rc"] == 0, report
    assert results["node_fetch_intercepted"]["rc"] == 0, report
    assert results["git_ls_remote"]["rc"] == 0, report
    assert results["dns_private"]["out"] == str(PLACEHOLDER_NET[1]), report
    assert results["curl_private"]["rc"] == 0, report
    assert results["curl_private"]["out"] == "200", report
    assert _failed(results, "tcp_host_direct"), report


async def test_enabled_guest_egress(tmp_path: Path, workspace: Path) -> None:
    settings = _settings(tmp_path)
    (workspace / "egress_probe.py").write_text(ENABLED_PROBE_SOURCE)
    proc = await spawn_microvm_pi(settings, cwd=str(workspace), tools=True)
    try:
        results = await _probe(proc)
    finally:
        await proc.terminate()
    report = json.dumps(results, indent=2)
    assert results["curl_public"]["rc"] == 0, report
    assert results["curl_ip_literal"]["rc"] == 0, report
    assert _failed(results, "curl_metadata"), report
    assert _failed(results, "curl_private_resolve"), report


ENABLED_PRIVATE_PROBE_SOURCE = r"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

private = json.loads(Path("/workspace/egress_probe.json").read_text())["private"]
results = {}
curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20"]


def run(name, argv):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    results[name] = {"rc": done.returncode, "out": done.stdout, "err": done.stderr}


def check(name, func):
    try:
        results[name] = {"rc": 0, "out": str(func()), "err": ""}
    except Exception as exc:
        results[name] = {"rc": 1, "out": "", "err": repr(exc)}


check("dns_private", lambda: socket.getaddrinfo(private, 443)[0][4][0])
check("dns_public", lambda: socket.getaddrinfo("example.com", 443)[0][4][0])
run("git_clone", ["git", "clone", f"https://{private}/org/repo.git", "/tmp/repo"])
check("readme", lambda: Path("/tmp/repo/README").read_text())
run("curl_public", [*curl, "https://example.com/"])
placeholder = results["dns_private"]["out"] or "198.18.0.1"
run(
    "curl_placeholder_other_name",
    [
        *curl,
        "-k",
        "--resolve",
        f"example.com:443:{placeholder}",
        "https://example.com/",
    ],
)
print(json.dumps({"type": "egress_probe", "results": results}), flush=True)
sys.stdin.read()
"""


class _PrivateGit(GitServer):
    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(
                self._serve, "127.0.0.1", 443, ssl=self.ca.server_context(PRIVATE)
            )
        except OSError as exc:
            pytest.skip(f"cannot bind 127.0.0.1:443 for the private git server: {exc}")


async def test_enabled_guest_clones_from_a_private_credential_host(
    tmp_path: Path, workspace: Path
) -> None:
    if BACKEND is None:
        pytest.skip("needs git and git-http-backend")
    upstream_ca = WorkerCA()
    ca_file = tmp_path / "private-ca.pem"
    ca_file.write_bytes(upstream_ca.cert_pem)
    settings = _settings(
        tmp_path,
        microvm_egress_private_hosts=PRIVATE,
        microvm_egress_upstream_ca=str(ca_file),
    )
    bare_repo(tmp_path)
    server = _PrivateGit(tmp_path / "srv", upstream_ca)
    await server.start()
    (workspace / "egress_probe.py").write_text(ENABLED_PRIVATE_PROBE_SOURCE)
    (workspace / "egress_probe.json").write_text(json.dumps({"private": PRIVATE}))
    credential = ContextEnvCredential(
        credential_id="cred_git",
        secret_name="GIT_TOKEN",
        secret_value=SECRET,
        allowed_hosts=[PRIVATE],
    )
    try:
        proc = await spawn_microvm_pi(
            settings, cwd=str(workspace), tools=True, env_credentials=[credential]
        )
        try:
            results = await _probe(proc)
        finally:
            await proc.terminate()
    finally:
        server.close()
    report = json.dumps(results, indent=2)
    assert results["dns_private"]["out"] == str(PLACEHOLDER_NET[1]), report
    assert results["dns_public"]["rc"] == 0, report
    assert results["dns_public"]["out"] != str(PLACEHOLDER_NET[1]), report
    assert results["git_clone"]["rc"] == 0, report
    assert results["readme"]["out"] == "hello\n", report
    assert SECRET not in report
    assert server.auth, report
    assert results["curl_public"]["rc"] == 0, report
    assert _failed(results, "curl_placeholder_other_name"), report


DISABLED_PROBE_SOURCE = r"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

net = dict(
    line.split("=", 1)
    for line in Path("/workspace/.apipi/net").read_text().split()
    if "=" in line
)
gateway = net.get("GUEST_GW", "").strip("'")
results = {}
curl = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "15"]


def run(name, argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=40)
    results[name] = {"rc": done.returncode, "out": done.stdout, "err": done.stderr}


def check(name, func):
    try:
        results[name] = {"rc": 0, "out": str(func()), "err": ""}
    except Exception as exc:
        results[name] = {"rc": 1, "out": "", "err": repr(exc)}


def dns_query():
    query = bytes.fromhex("123401000001000000000000076578616d706c6503636f6d0000010001")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(5)
        udp.sendto(query, ("1.1.1.1", 53))
        return udp.recv(512).hex()


def tcp(host, port):
    with socket.create_connection((host, port), timeout=8):
        return "connected"


run("curl_public", [*curl, "https://example.com/"])
run("curl_ip_literal", [*curl, "https://1.1.1.1/"])
check("dns_direct", dns_query)
check("tcp_dns_direct", lambda: tcp("1.1.1.1", 53))
check("tcp_host_other_port", lambda: tcp(gateway, 22))
run("curl_broker", [*curl, os.environ["OPENAI_BASE_URL"] + "/models"])
print(json.dumps({"type": "egress_probe", "results": results}), flush=True)
sys.stdin.read()
"""


async def test_disabled_guest_reaches_only_the_broker(
    tmp_path: Path, workspace: Path
) -> None:
    settings = _settings(tmp_path)
    (workspace / "egress_probe.py").write_text(DISABLED_PROBE_SOURCE)
    write_network_policy(workspace, NetworkPolicy(access="disabled"))
    proc = await spawn_microvm_pi(settings, cwd=str(workspace), tools=True)
    try:
        results = await _probe(proc)
    finally:
        await proc.terminate()
    report = json.dumps(results, indent=2)
    assert _failed(results, "curl_public"), report
    assert _failed(results, "curl_ip_literal"), report
    assert _failed(results, "dns_direct"), report
    assert _failed(results, "tcp_dns_direct"), report
    assert _failed(results, "tcp_host_other_port"), report
    assert results["curl_broker"]["rc"] == 0, report
