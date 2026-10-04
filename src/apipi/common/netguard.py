import ipaddress

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


def norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def allowed_networks(allow_hosts: tuple[str, ...]) -> tuple[Network, ...]:
    networks: list[Network] = []
    for entry in allow_hosts:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


def allowed_names(allow_hosts: tuple[str, ...]) -> frozenset[str]:
    names: set[str] = set()
    for entry in allow_hosts:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            names.add(norm_host(entry))
    return frozenset(names)


def ip_blocked(ip: Address) -> bool:
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
