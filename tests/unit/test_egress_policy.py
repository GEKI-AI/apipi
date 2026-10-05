import ipaddress
from typing import Any, cast

import pytest

from apipi.worker.egress.policy import (
    PLACEHOLDER_NET,
    Decision,
    EgressPolicy,
    is_placeholder,
    split_host_port,
    valid_hostname,
)
from apipi.worker.egress.resolve import (
    BLOCKED_EGRESS_CIDRS,
    EgressBlocked,
    SystemResolver,
    resolve_upstream,
)


def test_restricted_allows_only_listed_names() -> None:
    policy = EgressPolicy.build("restricted", allowed_hosts=("API.example.com.",))
    assert policy.decide("api.example.com", 443) == Decision("splice")
    assert policy.decide("Api.Example.Com", 80) == Decision("splice")
    assert policy.decide("www.example.com", 443) == Decision("reject", "not_allowed")
    assert policy.decide("example.com", 443) == Decision("reject", "not_allowed")
    assert policy.decide("1.1.1.1", 443) == Decision("reject", "ip_literal")
    assert policy.decide("[2001:db8::1]", 443) == Decision("reject", "ip_literal")
    assert policy.decide(None, 443) == Decision("reject", "no_host")
    assert policy.allows_name("api.example.com")
    assert not policy.allows_name("other.example.com")


def test_enabled_allows_public_names_and_ip_literals() -> None:
    policy = EgressPolicy.build("enabled")
    assert policy.decide("anything.example", 443) == Decision("splice")
    assert policy.decide("1.1.1.1", 443) == Decision("splice")
    assert policy.decide(None, 443) == Decision("splice")
    assert policy.allows_name("anything.example")


def test_disabled_rejects_everything() -> None:
    policy = EgressPolicy.build("disabled", allowed_hosts=("api.example.com",))
    assert policy.decide("api.example.com", 443) == Decision("reject", "disabled")
    assert policy.decide(None, 80) == Decision("reject", "disabled")
    assert not policy.allows_name("api.example.com")


def test_intercept_set() -> None:
    policy = EgressPolicy.build(
        "restricted",
        allowed_hosts=("api.example.com", "cdn.example.com"),
        intercept_hosts=("API.example.com",),
    )
    assert policy.decide("api.example.com", 443) == Decision("intercept")
    assert policy.decide("cdn.example.com", 443) == Decision("splice")
    enabled = EgressPolicy.build("enabled").with_intercept(["git.example.com"])
    assert enabled.decide("git.example.com", 443) == Decision("intercept")
    assert enabled.decide("1.1.1.1", 443) == Decision("splice")
    assert enabled.with_intercept([]).decide("git.example.com", 443) == Decision(
        "splice"
    )


def test_private_hosts_only_for_named_hosts() -> None:
    restricted = EgressPolicy.build(
        "restricted", allowed_hosts=("git.internal",), private_hosts=("git.internal",)
    )
    assert restricted.private_allowed("Git.Internal")
    assert not restricted.private_allowed("other.internal")
    assert not restricted.private_allowed("10.1.2.3")
    assert not restricted.private_allowed(None)
    enabled = EgressPolicy.build("enabled", private_hosts=("git.internal",))
    assert not enabled.private_allowed("git.internal")
    assert enabled.with_intercept(["git.internal"]).private_allowed("git.internal")


@pytest.mark.timeout(300)
def test_each_private_name_gets_its_own_placeholder() -> None:
    policy = EgressPolicy.build(
        "restricted",
        allowed_hosts=("git.internal", "wiki.internal", "api.example.com"),
        private_hosts=(
            "zz.internal",
            "wiki.internal",
            "git.internal",
            "aa.internal",
            "10.0.0.0/8",
        ),
    )
    assert policy.placeholder("Git.Internal") == "198.18.0.1"
    assert policy.placeholder("wiki.internal") == "198.18.0.2"
    assert policy.placeholder("aa.internal") is None
    assert policy.placeholder("api.example.com") is None
    many = EgressPolicy.build(
        "restricted",
        allowed_hosts=[f"h{i:03}.internal" for i in range(300)],
        private_hosts=[f"h{i:03}.internal" for i in range(300)],
    )
    assert many.placeholder("h299.internal") == "198.18.1.44"
    assert all(
        ipaddress.ip_address(many.placeholder(f"h{i:03}.internal") or "")
        in PLACEHOLDER_NET
        for i in range(300)
    )


def test_private_name_needs_the_session_and_the_operator() -> None:
    restricted = EgressPolicy.build(
        "restricted",
        allowed_hosts=("git.internal", "api.example.com"),
        private_hosts=("git.internal", "wiki.internal", "10.0.0.0/8"),
    )
    assert restricted.private_name("Git.Internal.")
    assert not restricted.private_name("wiki.internal")
    assert not restricted.private_name("api.example.com")
    assert not restricted.private_name("10.0.0.0/8")
    enabled = EgressPolicy.build("enabled", private_hosts=("git.internal",))
    assert not enabled.private_name("git.internal")
    disabled = EgressPolicy.build(
        "disabled", allowed_hosts=("git.internal",), private_hosts=("git.internal",)
    )
    assert not disabled.private_name("git.internal")


def test_enabled_private_names_are_its_private_credential_hosts() -> None:
    plain = EgressPolicy.build("enabled", private_hosts=("git.internal",))
    assert plain.private_names() == ()
    assert not plain.needs_dns()
    assert plain.placeholder("git.internal") is None
    public = plain.with_intercept(["github.com"])
    assert public.private_names() == ()
    assert not public.needs_dns()
    enabled = EgressPolicy.build(
        "enabled",
        private_hosts=("wiki.internal", "git.internal", "10.0.0.0/8"),
        intercept_hosts=("Git.Internal", "github.com"),
    )
    assert enabled.private_names() == ("git.internal",)
    assert enabled.needs_dns()
    assert enabled.placeholder("git.internal") == "198.18.0.1"
    assert enabled.placeholder("wiki.internal") is None
    assert enabled.placeholder("github.com") is None
    assert EgressPolicy.build("restricted", allowed_hosts=("a.test",)).needs_dns()
    disabled = EgressPolicy.build(
        "disabled", private_hosts=("git.internal",), intercept_hosts=("git.internal",)
    )
    assert not disabled.needs_dns()


def test_placeholder_name_maps_an_address_back() -> None:
    policy = EgressPolicy.build(
        "restricted",
        allowed_hosts=("git.internal", "wiki.internal"),
        private_hosts=("wiki.internal", "git.internal", "aa.internal"),
    )
    for name in policy.private_names():
        address = policy.placeholder(name)
        assert address is not None
        assert is_placeholder(address)
        assert policy.placeholder_name(address) == name
    for address in ("198.18.0.0", "198.18.0.3", "198.19.255.255", "93.184.216.34"):
        assert policy.placeholder_name(address) is None
    assert policy.placeholder_name("::ffff:198.18.0.1") is None
    assert policy.placeholder_name("not an address") is None
    assert not is_placeholder("10.0.0.1")
    assert not is_placeholder("2001:db8::1")


def test_hostnames_are_validated() -> None:
    assert valid_hostname("api.example.com")
    assert valid_hostname("_dmarc.example.com")
    assert not valid_hostname("a" * 64 + ".example.com")
    assert not valid_hostname("bad host.example")
    assert not valid_hostname("-lead.example")
    assert not valid_hostname("x." * 130 + "com")
    policy = EgressPolicy.build("enabled")
    assert policy.decide("a" * 64 + ".example", 443) == Decision("reject", "bad_host")
    assert policy.decide("ex\u00e4mple.com", 443) == Decision("reject", "bad_host")


def test_unknown_mode() -> None:
    with pytest.raises(ValueError, match="unknown egress mode"):
        EgressPolicy.build(cast(Any, "open"))


def test_split_host_port() -> None:
    assert split_host_port("api.example.com") == ("api.example.com", None)
    assert split_host_port("api.example.com:8443") == ("api.example.com", 8443)
    assert split_host_port("[2001:db8::1]:443") == ("2001:db8::1", 443)
    assert split_host_port("[2001:db8::1]") == ("2001:db8::1", None)
    with pytest.raises(ValueError):
        split_host_port("api.example.com:http")


def test_blocked_cidrs_cover_private_ranges() -> None:
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "127.0.0.0/8",
    ):
        assert cidr in BLOCKED_EGRESS_CIDRS
    assert all(":" not in cidr for cidr in BLOCKED_EGRESS_CIDRS)


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str, _port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


async def test_resolve_rejects_any_private_address() -> None:
    resolve = _resolver(
        {
            "public.example": ["93.184.216.34"],
            "rebind.example": ["93.184.216.34", "10.0.0.5"],
            "metadata.example": ["169.254.169.254"],
            "v6.example": ["fd00::1"],
        }
    )
    assert await resolve_upstream("public.example", 443, resolve=resolve) == [
        "93.184.216.34"
    ]
    for host in ("rebind.example", "metadata.example", "v6.example"):
        with pytest.raises(EgressBlocked) as err:
            await resolve_upstream(host, 443, resolve=resolve)
        assert err.value.reason == "private_address"
    with pytest.raises(EgressBlocked) as err:
        await resolve_upstream("missing.example", 443, resolve=resolve)
    assert err.value.reason == "resolve_failed"
    with pytest.raises(EgressBlocked) as err:
        await resolve_upstream("192.168.1.1", 443, resolve=resolve)
    assert err.value.reason == "private_address"
    assert await resolve_upstream("1.1.1.1", 443, resolve=resolve) == ["1.1.1.1"]


async def _blocked_reason(host: str, private: tuple[str, ...], resolve) -> str:
    with pytest.raises(EgressBlocked) as err:
        await resolve_upstream(host, 443, private_hosts=private, resolve=resolve)
    return err.value.reason


async def test_private_hosts_need_the_name() -> None:
    resolve = _resolver(
        {
            "forgejo.internal": ["10.1.2.3"],
            "10-0-0-5.nip.io": ["10.0.0.5"],
            "net.internal": ["192.168.7.7"],
        }
    )
    named = ("Forgejo.Internal",)
    assert await resolve_upstream(
        "forgejo.internal", 443, private_hosts=named, resolve=resolve
    ) == ["10.1.2.3"]
    cidr_only = ("10.0.0.0/8", "192.168.7.0/24")
    for host in ("10-0-0-5.nip.io", "net.internal", "forgejo.internal"):
        assert await _blocked_reason(host, cidr_only, resolve) == "private_address"
    assert await _blocked_reason("192.168.7.8", cidr_only, resolve) == (
        "private_address"
    )


async def test_cidrs_restrict_named_private_hosts() -> None:
    resolve = _resolver(
        {
            "forgejo.internal": ["10.1.2.3"],
            "moved.internal": ["10.9.9.9"],
        }
    )
    private = ("forgejo.internal", "moved.internal", "10.1.0.0/16")
    assert await resolve_upstream(
        "forgejo.internal", 443, private_hosts=private, resolve=resolve
    ) == ["10.1.2.3"]
    assert await _blocked_reason("moved.internal", private, resolve) == (
        "private_address"
    )


async def test_named_private_hosts_never_reach_metadata() -> None:
    resolve = _resolver(
        {
            "meta.internal": ["169.254.169.254"],
            "link.internal": ["169.254.10.10"],
            "v6meta.internal": ["fd00:ec2::254"],
            "local.internal": ["127.0.0.1"],
        }
    )
    private = ("meta.internal", "link.internal", "v6meta.internal", "local.internal")
    for host in ("meta.internal", "link.internal", "v6meta.internal"):
        assert await _blocked_reason(host, private, resolve) == "private_address"
    assert await resolve_upstream(
        "local.internal", 443, private_hosts=private, resolve=resolve
    ) == ["127.0.0.1"]


async def test_resolve_maps_bad_names_to_resolve_failed() -> None:
    async def broken(host: str, _port: int) -> list[str]:
        raise UnicodeError("label too long")

    with pytest.raises(EgressBlocked) as err:
        await resolve_upstream("a" * 64 + ".example", 443, resolve=broken)
    assert err.value.reason == "resolve_failed"
    system = SystemResolver()
    try:
        with pytest.raises(EgressBlocked) as err:
            await resolve_upstream("a" * 64 + ".example", 443, resolve=system)
    finally:
        system.close()
    assert err.value.reason == "resolve_failed"
