import asyncio
import base64
import contextlib
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import h11
import pytest

from apipi.worker.egress import EgressGateway, EgressPolicy, WorkerCA
from apipi.worker.egress.inject import Injection, SecretInjector

HOST = "allowed.test"
PORT = 8443
SECRET = "ghp_real_secret_value"
PLACEHOLDER = "apipi-secret-" + "d" * 32
HELPER = Path(__file__).parents[2] / "src/apipi/worker/pi/git-credential.sh"
BACKEND = Path("/usr/lib/git-core/git-http-backend")

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not BACKEND.is_file(),
    reason="needs git and git-http-backend",
)


def _free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


@dataclass
class GitServer:
    root: Path
    ca: WorkerCA
    auth: list[str] = field(default_factory=list)
    server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._serve, "127.0.0.2", PORT, ssl=self.ca.server_context(HOST)
        )

    def close(self) -> None:
        if self.server is not None:
            self.server.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = h11.Connection(h11.SERVER)
        try:
            while True:
                request: h11.Request | None = None
                body = bytearray()
                while True:
                    event = conn.next_event()
                    if event is h11.NEED_DATA:
                        conn.receive_data(await reader.read(65536))
                        continue
                    if isinstance(event, h11.ConnectionClosed):
                        return
                    if isinstance(event, h11.Request):
                        request = event
                    elif isinstance(event, h11.Data):
                        body.extend(event.data)
                    elif isinstance(event, h11.EndOfMessage):
                        break
                assert request is not None
                status, headers, data = await self._handle(request, bytes(body))
                writer.write(
                    conn.send(
                        h11.Response(
                            status_code=status,
                            headers=[*headers, ("content-length", str(len(data)))],
                        )
                    )
                )
                writer.write(conn.send(h11.Data(data=data)))
                writer.write(conn.send(h11.EndOfMessage()))
                await writer.drain()
                if conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                    return
                conn.start_next_cycle()
        except (OSError, ConnectionError, h11.ProtocolError):
            return
        finally:
            writer.close()

    async def _handle(
        self, request: h11.Request, body: bytes
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        headers = {k.decode().lower(): v.decode() for k, v in request.headers}
        auth = headers.get("authorization")
        if auth is not None:
            self.auth.append(auth)
        expected = "Basic " + base64.b64encode(
            f"x-access-token:{SECRET}".encode()
        ).decode("ascii")
        if auth != expected:
            return 401, [("www-authenticate", 'Basic realm="git"')], b"auth\n"
        path, _, query = request.target.decode().partition("?")
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": str(self.root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": request.method.decode(),
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_TYPE": headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": "x-access-token",
            "REMOTE_ADDR": "127.0.0.1",
        }
        for name in ("git-protocol", "content-encoding"):
            if name in headers:
                env["HTTP_" + name.upper().replace("-", "_")] = headers[name]
        proc = await asyncio.create_subprocess_exec(
            str(BACKEND),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )
        out, _ = await proc.communicate(body)
        head, _, data = out.partition(b"\r\n\r\n")
        status = 200
        response: list[tuple[str, str]] = []
        for line in head.decode().split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            elif name:
                response.append((name.strip(), value.strip()))
        return status, response, data


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


def _run(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _bare_repo(root: Path) -> None:
    work = root / "seed"
    bare = root / "srv" / "org" / "repo.git"
    bare.parent.mkdir(parents=True)
    _run("init", "--bare", "-b", "main", str(bare))
    _run("-C", str(bare), "config", "http.receivepack", "true")
    _run("init", "-b", "main", str(work))
    (work / "README").write_text("hello\n")
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    _run("-C", str(work), "add", "README")
    _run("-C", str(work), *ident, "commit", "-m", "init")
    _run("-C", str(work), "push", str(bare), "main")


async def test_git_clone_and_push_through_gateway(tmp_path: Path) -> None:
    if not (_free("127.0.0.1", PORT) and _free("127.0.0.2", PORT)):
        pytest.skip(f"port {PORT} is busy")
    _bare_repo(tmp_path)
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
            private_hosts=("127.0.0.0/8",),
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
