import asyncio
import functools
import ipaddress
import socket
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from apipi.common.netguard import (
    BLOCKED_NETWORKS,
    METADATA_IPS,
    Address,
    allowed_names,
    allowed_networks,
    ip_blocked,
)
from apipi.worker.egress.policy import norm_host
from apipi.worker.egress.sni import is_ip_literal

BLOCKED_EGRESS_CIDRS: tuple[str, ...] = tuple(
    str(network) for network in BLOCKED_NETWORKS if network.version == 4
)

RESOLVE_THREADS = 8
Resolver = Callable[[str, int], Awaitable[list[str]]]
Blocked = Callable[[str], bool]

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def resolver_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=RESOLVE_THREADS, thread_name_prefix="apipi-egress-dns"
            )
        return _executor


class EgressBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def system_resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    lookup = functools.partial(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
    infos = await loop.run_in_executor(resolver_executor(), lookup)
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


def metadata_address(ip: Address) -> bool:
    checked: list[Address] = [ip]
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        checked.append(mapped)
    return any(str(item) in METADATA_IPS or item.is_link_local for item in checked)


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
    named = name in allowed_names(private_hosts)
    networks = allowed_networks(private_hosts)
    for address in addresses:
        if not blocked(address):
            continue
        if not named:
            raise EgressBlocked("private_address")
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise EgressBlocked("private_address") from None
        if metadata_address(ip):
            raise EgressBlocked("private_address")
        if networks and not any(ip in network for network in networks):
            raise EgressBlocked("private_address")
    return addresses
