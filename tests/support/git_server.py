import asyncio
import base64
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import h11

from apipi.worker.egress import WorkerCA
from tests.support.egress import HOST

PORT = 8443
SECRET = "ghp_real_secret_value"


def _backend() -> Path | None:
    if shutil.which("git") is None:
        return None
    found = subprocess.run(
        ["git", "--exec-path"], capture_output=True, text=True, check=False
    )
    path = Path(found.stdout.strip()) / "git-http-backend"
    return path if found.returncode == 0 and path.is_file() else None


BACKEND = _backend()


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


def _run(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def bare_repo(root: Path) -> None:
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
