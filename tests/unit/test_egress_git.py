import asyncio
import base64
import contextlib
import os
import socket
import subprocess
from pathlib import Path

import pytest
from tests.support.egress import HOST
from tests.support.git_server import BACKEND, PORT, SECRET, GitServer, bare_repo

from apipi.worker.egress import EgressGateway, EgressPolicy, WorkerCA
from apipi.worker.egress.inject import Injection, SecretInjector

PLACEHOLDER = "apipi-secret-" + "d" * 32
HELPER = Path(__file__).parents[2] / "src/apipi/worker/pi/git-credential.sh"

pytestmark = pytest.mark.skipif(
    BACKEND is None, reason="needs git and git-http-backend"
)


def _bind_error(host: str, port: int) -> str | None:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            return f"cannot bind {host}:{port}: {exc.strerror or exc}"
    return None


async def _forward(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int
) -> None:
    up_reader, up_writer = await asyncio.open_connection("127.0.0.1", port)

    async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await src.read(65536):
                dst.write(data)
                await dst.drain()
        with contextlib.suppress(Exception):
            dst.close()

    await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def _git(
    *args: str, cwd: Path, env: dict[str, str], stdin: str | None = None
) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(
        proc.communicate(stdin.encode() if stdin is not None else None), timeout=60
    )
    assert proc.returncode == 0, err.decode()
    return out.decode()


async def test_git_clone_and_push_through_gateway(tmp_path: Path) -> None:
    for host in ("127.0.0.1", "127.0.0.2"):
        error = _bind_error(host, PORT)
        if error is not None:
            pytest.skip(error)
    bare_repo(tmp_path)
    worker_ca = WorkerCA()
    upstream_ca = WorkerCA()
    ca_file = tmp_path / "upstream-ca.pem"
    ca_file.write_bytes(upstream_ca.cert_pem)
    guest_ca = tmp_path / "guest-ca.pem"
    guest_ca.write_bytes(worker_ca.cert_pem)
    server = GitServer(tmp_path / "srv", upstream_ca)
    await server.start()
    injector = SecretInjector(
        [Injection("cred", "GITHUB_TOKEN", PLACEHOLDER, SECRET, (HOST,))]
    )

    async def resolve(host: str, _port: int) -> list[str]:
        assert host == HOST
        return ["127.0.0.2"]

    gateway = EgressGateway(
        host="127.0.0.1",
        policy=EgressPolicy.build(
            "restricted",
            allowed_hosts=(HOST,),
            private_hosts=(HOST, "127.0.0.0/8"),
            intercept_hosts=(HOST,),
        ),
        ca=worker_ca,
        upstream_ca=str(ca_file),
        hooks=injector.hooks(),
        resolve=resolve,
        original_dst=lambda _sock: ("198.51.100.7", PORT),
        tls_ports=frozenset({PORT}),
        http_ports=frozenset(),
    )
    await gateway.start()
    dnat = await asyncio.start_server(
        lambda r, w: _forward(r, w, gateway.port), "127.0.0.1", PORT
    )
    helper = tmp_path / "git-credential"
    helper.write_bytes(HELPER.read_bytes())
    helper.chmod(0o755)
    hosts = tmp_path / "git-credentials"
    hosts.write_bytes(injector.git_credentials())
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSL_CAINFO": str(guest_ca),
        "GIT_CONFIG_KEY_0": "http.curloptResolve",
        "GIT_CONFIG_VALUE_0": f"{HOST}:{PORT}:127.0.0.1",
        **injector.git_config_env(f"{helper} {hosts}", start=1),
    }
    url = f"https://{HOST}:{PORT}/org/repo.git"
    try:
        listed = await _git("ls-remote", url, cwd=tmp_path, env=env)
        assert "refs/heads/main" in listed
        await _git("clone", url, "clone", cwd=tmp_path, env=env)
        work = tmp_path / "clone"
        assert (work / "README").read_text() == "hello\n"
        (work / "NEW").write_text("pushed\n")
        await _git("add", "NEW", cwd=work, env=env)
        await _git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-m",
            "new",
            cwd=work,
            env=env,
        )
        await _git("push", "origin", "main", cwd=work, env=env)
        log = subprocess.run(
            ["git", "-C", str(tmp_path / "srv/org/repo.git"), "log", "--oneline"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "new" in log
        remote = await _git("config", "remote.origin.url", cwd=work, env=env)
        assert remote.strip() == url
        config = (work / ".git" / "config").read_text()
        assert SECRET not in config
        assert PLACEHOLDER not in config
        rewritten = await _git(
            "ls-remote", "--get-url", f"git@{HOST}:org/repo.git", cwd=tmp_path, env=env
        )
        assert rewritten.strip() == f"https://{HOST}/org/repo.git"
        ssh = await _git(
            "ls-remote",
            "--get-url",
            f"ssh://git@{HOST}/org/repo.git",
            cwd=tmp_path,
            env=env,
        )
        assert ssh.strip() == f"https://{HOST}/org/repo.git"
        filled = await _git(
            "credential",
            "fill",
            cwd=tmp_path,
            env=env,
            stdin=f"url=https://{HOST}:8443/org/repo.git\n\n",
        )
        assert f"password={PLACEHOLDER}" in filled
        assert "username=x-access-token" in filled
    finally:
        dnat.close()
        await gateway.stop()
        server.close()
    expected = "Basic " + base64.b64encode(f"x-access-token:{SECRET}".encode()).decode()
    assert server.auth
    assert set(server.auth) == {expected}
