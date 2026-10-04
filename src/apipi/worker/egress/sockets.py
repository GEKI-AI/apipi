import asyncio
import contextlib
import socket
import ssl

CHUNK = 65536
CONNECT_TIMEOUT = 10.0
IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)
SO_ORIGINAL_DST = 80


class UpstreamError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def bind_socket(
    host: str, kind: socket.SocketKind, *, freebind: bool = False
) -> socket.socket:
    sock = socket.socket(socket.AF_INET, kind)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if freebind:
            sock.setsockopt(socket.SOL_IP, IP_FREEBIND, 1)
        sock.bind((host, 0))
        if kind == socket.SOCK_STREAM:
            sock.listen(512)
        sock.setblocking(False)
    except OSError:
        sock.close()
        raise
    return sock


def original_dst(sock: socket.socket) -> tuple[str, int]:
    raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
    port = int.from_bytes(raw[2:4], "big")
    return socket.inet_ntoa(raw[4:8]), port


async def accept_stream(
    sock: socket.socket,
    *,
    ssl_context: ssl.SSLContext | None = None,
    handshake_timeout: float | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2 * CHUNK, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    transport, _ = await loop.connect_accepted_socket(
        lambda: protocol,
        sock,
        ssl=ssl_context,
        ssl_handshake_timeout=handshake_timeout if ssl_context is not None else None,
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return reader, writer


async def open_upstream(
    addresses: list[str],
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
    server_hostname: str | None = None,
    timeout: float = CONNECT_TIMEOUT,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    for address in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(
                    address,
                    port,
                    ssl=ssl_context,
                    server_hostname=server_hostname if ssl_context else None,
                    limit=2 * CHUNK,
                ),
                timeout=timeout,
            )
        except ssl.SSLCertVerificationError as exc:
            raise UpstreamError("upstream_tls") from exc
        except (OSError, TimeoutError):
            continue
    raise UpstreamError("upstream_unreachable")


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
