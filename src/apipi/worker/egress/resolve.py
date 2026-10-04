import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

from apipi.common.netguard import (
    BLOCKED_NETWORKS,
    allowed_names,
    allowed_networks,
    ip_blocked,
)
from apipi.worker.egress.policy import norm_host
from apipi.worker.egress.sni import is_ip_literal

BLOCKED_EGRESS_CIDRS: tuple[str, ...] = tuple(
    str(network) for network in BLOCKED_NETWORKS if network.version == 4
)

Resolver = Callable[[str, int], Awaitable[list[str]]]
Blocked = Callable[[str], bool]


class EgressBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    found: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in found:
            found.append(address)
    return found


def address_blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return ip_blocked(ip)


def check_address(address: str, *, blocked: Blocked = address_blocked) -> list[str]:
    if blocked(address):
        raise EgressBlocked("private_address")
    return [address]


async def resolve_upstream(
    host: str,
    port: int,
    *,
    private_hosts: tuple[str, ...] = (),
    resolve: Resolver = system_resolve,
    blocked: Blocked = address_blocked,
) -> list[str]:
    name = norm_host(host)
    if is_ip_literal(name):
        return check_address(name.strip("[]"), blocked=blocked)
    try:
        addresses = await resolve(name, port)
    except (OSError, UnicodeError, ValueError) as exc:
        raise EgressBlocked("resolve_failed") from exc
    if not addresses:
        raise EgressBlocked("resolve_failed")
    if name in allowed_names(private_hosts):
        return addresses
    networks = allowed_networks(private_hosts)
    for address in addresses:
        if not blocked(address):
            continue
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise EgressBlocked("private_address") from None
        if not any(ip in network for network in networks):
            raise EgressBlocked("private_address")
    return addresses
