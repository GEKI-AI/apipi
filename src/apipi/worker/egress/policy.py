import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Literal

from apipi.worker.egress.sni import is_ip_literal

EgressMode = Literal["enabled", "restricted", "disabled"]
Action = Literal["splice", "intercept", "reject"]
EGRESS_MODES: tuple[EgressMode, ...] = ("enabled", "restricted", "disabled")
_LABEL = re.compile(r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?")


def norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def valid_hostname(host: str) -> bool:
    name = norm_host(host)
    if not name or len(name) > 253:
        return False
    return all(_LABEL.fullmatch(label) for label in name.split("."))


def split_host_port(value: str) -> tuple[str, int | None]:
    text = value.strip()
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            return text, None
        host = text[1:end]
        rest = text[end + 1 :]
    elif text.count(":") == 1:
        host, _, rest = text.partition(":")
        rest = ":" + rest
    else:
        return text, None
    if not rest:
        return host, None
    if not rest.startswith(":") or not rest[1:].isdigit():
        raise ValueError(f"invalid host: {value}")
    return host, int(rest[1:])


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""


@dataclass(frozen=True)
class EgressPolicy:
    mode: EgressMode
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    private_hosts: tuple[str, ...] = ()
    intercept_hosts: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def build(
        cls,
        mode: EgressMode,
        *,
        allowed_hosts: Iterable[str] = (),
        private_hosts: Iterable[str] = (),
        intercept_hosts: Iterable[str] = (),
    ) -> "EgressPolicy":
        if mode not in EGRESS_MODES:
            raise ValueError(f"unknown egress mode: {mode}")
        return cls(
            mode=mode,
            allowed_hosts=frozenset(norm_host(host) for host in allowed_hosts),
            private_hosts=tuple(private_hosts),
            intercept_hosts=frozenset(norm_host(host) for host in intercept_hosts),
        )

    def with_intercept(self, hosts: Iterable[str]) -> "EgressPolicy":
        return replace(
            self, intercept_hosts=frozenset(norm_host(host) for host in hosts)
        )

    def allows_name(self, host: str) -> bool:
        if self.mode == "disabled":
            return False
        if self.mode == "enabled":
            return True
        return norm_host(host) in self.allowed_hosts

    def private_allowed(self, host: str | None) -> bool:
        if not host:
            return False
        name = norm_host(host)
        if is_ip_literal(name):
            return False
        if name in self.intercept_hosts:
            return True
        return self.mode == "restricted" and name in self.allowed_hosts

    def decide(self, host: str | None, port: int) -> Decision:
        if self.mode == "disabled":
            return Decision("reject", "disabled")
        if host is None or not host:
            if self.mode == "restricted":
                return Decision("reject", "no_host")
            return Decision("splice")
        name = norm_host(host)
        if is_ip_literal(name):
            if self.mode == "restricted":
                return Decision("reject", "ip_literal")
            return Decision("splice")
        if not valid_hostname(name):
            return Decision("reject", "bad_host")
        if self.mode == "restricted" and name not in self.allowed_hosts:
            return Decision("reject", "not_allowed")
        if name in self.intercept_hosts:
            return Decision("intercept")
        return Decision("splice")
