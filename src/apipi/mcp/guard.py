import asyncio
import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


class McpConnectError(Exception):
    pass


METADATA_HOSTNAMES = frozenset({"metadata.google.internal"})

METADATA_IPS = frozenset(
    {
        "169.254.169.254",
        "169.254.169.123",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address

BLOCKED_NETWORKS: tuple[Network, ...] = tuple(
    ipaddress.ip_network(item)
    for item in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "::/128",
        "64:ff9b::/96",
        "100::/64",
        "2001::/23",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
        "::ffff:0:0/96",
    )
)


def split_allow_hosts(raw: object) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw]
    else:
        items = [part.strip() for part in str(raw).split(",")]
    return tuple(item for item in items if item)


def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _allowed_networks(allow_hosts: tuple[str, ...]) -> tuple[Network, ...]:
    networks: list[Network] = []
    for entry in allow_hosts:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def _allowed_names(allow_hosts: tuple[str, ...]) -> frozenset[str]:
    names: set[str] = set()
    for entry in allow_hosts:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            names.add(_norm_host(entry))
    return frozenset(names)


def _ip_blocked(ip: Address) -> bool:
    if str(ip) in METADATA_IPS:
        return True
    if any(ip in net for net in BLOCKED_NETWORKS):
        return True
    checked: list[Address] = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        checked.append(mapped)
    return any(
        item.is_private
        or item.is_loopback
        or item.is_link_local
        or item.is_multicast
        or item.is_reserved
        or item.is_unspecified
        for item in checked
    )


def _check_ips(
    ips: list[str], *, url: str, allow_hosts: tuple[str, ...], label: str
) -> None:
    if not ips:
        raise McpConnectError(f"mcp {label} blocked host: {url}")
    networks = _allowed_networks(allow_hosts)
    for raw in ips:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            raise McpConnectError(f"mcp {label} blocked host: {url}") from None
        if _ip_blocked(ip) and not any(ip in net for net in networks):
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
    if _norm_host(host) in METADATA_HOSTNAMES:
        if _norm_host(host) not in _allowed_names(allow_hosts):
            raise McpConnectError(f"mcp {label} blocked host: {url}")
        return
    if _norm_host(host) in _allowed_names(allow_hosts):
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
    if _norm_host(host) in METADATA_HOSTNAMES:
        if _norm_host(host) not in _allowed_names(allow_hosts):
            raise McpConnectError(f"mcp {label} blocked host: {url}")
        return
    if _norm_host(host) not in _allowed_names(allow_hosts):
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
        ips = [str(info[4][0]) for info in infos]
        _check_ips(ips, url=url, allow_hosts=allow_hosts, label=label)
