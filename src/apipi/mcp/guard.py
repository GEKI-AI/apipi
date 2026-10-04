import asyncio
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

from apipi.common.netguard import (
    allowed_names,
    allowed_networks,
    ip_blocked,
    norm_host,
    split_allow_hosts,
)

__all__ = [
    "METADATA_HOSTNAMES",
    "McpConnectError",
    "check_mcp_url",
    "check_mcp_url_sync",
    "split_allow_hosts",
]


class McpConnectError(Exception):
    pass


METADATA_HOSTNAMES = frozenset({"metadata.google.internal"})


def _check_ips(
    ips: list[str], *, url: str, allow_hosts: tuple[str, ...], label: str
) -> None:
    if not ips:
        raise McpConnectError(f"mcp {label} blocked host: {url}")
    networks = allowed_networks(allow_hosts)
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise McpConnectError(f"mcp {label} blocked host: {url}") from None
        if ip_blocked(ip) and not any(ip in net for net in networks):
            raise McpConnectError(f"mcp {label} blocked host: {url}")


def _split_url(url: str, label: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise McpConnectError(f"mcp {label} blocked host: {url}")
    default = 443 if parsed.scheme == "https" else 80
    return parsed.hostname, url, parsed.port or default


def check_mcp_url_sync(
    url: str,
    *,
    label: str = "server",
    allow_hosts: tuple[str, ...] = (),
    resolve: Callable[[str], list[str]] | None = None,
) -> None:
    host, _, _ = _split_url(url, label)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _check_ips([host], url=url, allow_hosts=allow_hosts, label=label)
        return
    if norm_host(host) in METADATA_HOSTNAMES:
        if norm_host(host) not in allowed_names(allow_hosts):
            raise McpConnectError(f"mcp {label} blocked host: {url}")
        return
    if norm_host(host) in allowed_names(allow_hosts):
        return
    if resolve is not None:
        ips = list(resolve(host))
    else:
        _, _, port = _split_url(url, label)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        ips = [str(info[4][0]) for info in infos]
    _check_ips(ips, url=url, allow_hosts=allow_hosts, label=label)


async def check_mcp_url(
    url: str,
    *,
    label: str = "server",
    allow_hosts: tuple[str, ...] = (),
) -> None:
    host, _, port = _split_url(url, label)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _check_ips([host], url=url, allow_hosts=allow_hosts, label=label)
        return
    if norm_host(host) in METADATA_HOSTNAMES:
        if norm_host(host) not in allowed_names(allow_hosts):
            raise McpConnectError(f"mcp {label} blocked host: {url}")
        return
    if norm_host(host) not in allowed_names(allow_hosts):
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
        ips = [str(info[4][0]) for info in infos]
        _check_ips(ips, url=url, allow_hosts=allow_hosts, label=label)
