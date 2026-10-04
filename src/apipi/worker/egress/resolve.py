import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable

from apipi.mcp.guard import (
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


def address_allowed(address: str, private_hosts: tuple[str, ...]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not ip_blocked(ip):
        return True
    return any(ip in network for network in allowed_networks(private_hosts))


async def resolve_upstream(
    host: str,
    port: int,
    *,
    private_hosts: tuple[str, ...] = (),
    resolve: Resolver = system_resolve,
) -> list[str]:
    name = norm_host(host).strip("[]")
    if is_ip_literal(name):
        addresses = [name]
    else:
        try:
            addresses = await resolve(name, port)
        except OSError as exc:
            raise EgressBlocked("resolve_failed") from exc
    if not addresses:
        raise EgressBlocked("resolve_failed")
    if not is_ip_literal(name) and name in allowed_names(private_hosts):
        return addresses
    for address in addresses:
        if not address_allowed(address, private_hosts):
            raise EgressBlocked("private_address")
    return addresses
