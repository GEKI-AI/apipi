import base64
import binascii
import logging
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field

from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics
from apipi.protocol import ContextEnvCredential
from apipi.worker.egress.gateway import TLS_PORTS, egress_metrics
from apipi.worker.egress.intercept import (
    EgressHooks,
    Headers,
    RequestHead,
    ResponseHead,
)
from apipi.worker.egress.policy import norm_host

log = logging.getLogger("apipi.egress")

PLACEHOLDER_PREFIX = "apipi-secret-"
GIT_USERNAME_DEFAULTS = {"gitlab.com": "oauth2"}
GIT_USERNAME_FALLBACK = "x-access-token"


def new_placeholder() -> str:
    return PLACEHOLDER_PREFIX + secrets.token_hex(16)


@dataclass(frozen=True)
class Injection:
    credential_id: str
    secret_name: str
    placeholder: str
    value: str = field(repr=False)
    hosts: tuple[str, ...] = ()
    git_username: str | None = None

    def applies(self, host: str) -> bool:
        return norm_host(host) in self.hosts


def git_username(injection: Injection, host: str) -> str:
    if injection.git_username:
        return injection.git_username
    return GIT_USERNAME_DEFAULTS.get(norm_host(host), GIT_USERNAME_FALLBACK)


def _basic(value: str, injection: Injection) -> str | None:
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    placeholder = injection.placeholder.encode()
    if placeholder not in decoded:
        return None
    replaced = decoded.replace(placeholder, injection.value.encode())
    return f"{scheme} {base64.b64encode(replaced).decode('ascii')}"


def _query(target: str, injection: Injection) -> str | None:
    path, mark, query = target.partition("?")
    if not mark or injection.placeholder not in query:
        return None
    return f"{path}?{query.replace(injection.placeholder, injection.value)}"


class SecretInjector:
    def __init__(
        self,
        injections: Iterable[Injection],
        *,
        session_id: str | None = None,
        metrics: Metrics | None = None,
        ports: Iterable[int] = TLS_PORTS,
    ) -> None:
        self.injections = tuple(injections)
        self.session_id = session_id
        self.metrics = metrics
        self.ports = frozenset(ports)

    @property
    def hosts(self) -> tuple[str, ...]:
        found: list[str] = []
        for injection in self.injections:
            for host in injection.hosts:
                if host not in found:
                    found.append(host)
        return tuple(found)

    def guest_env(self) -> dict[str, str]:
        return {item.secret_name: item.placeholder for item in self.injections}

    def git_credentials(self) -> bytes:
        lines: list[str] = []
        seen: set[str] = set()
        for injection in self.injections:
            for host in injection.hosts:
                if host in seen:
                    continue
                seen.add(host)
                username = git_username(injection, host)
                lines.append(f"{host}\t{username}\t{injection.placeholder}\n")
        return "".join(lines).encode()

    def git_config_env(self, helper: str, start: int = 0) -> dict[str, str]:
        entries: list[tuple[str, str]] = []
        for host in self.hosts:
            entries.append((f"credential.https://{host}.helper", helper))
            entries.append((f"url.https://{host}/.insteadOf", f"git@{host}:"))
            entries.append((f"url.https://{host}/.insteadOf", f"ssh://git@{host}/"))
        if not entries:
            return {}
        env: dict[str, str] = {}
        for offset, (key, value) in enumerate(entries):
            env[f"GIT_CONFIG_KEY_{start + offset}"] = key
            env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
        env["GIT_CONFIG_COUNT"] = str(start + len(entries))
        return env

    def secret_values(self) -> tuple[str, ...]:
        return tuple(item.value for item in self.injections)

    def hooks(self) -> EgressHooks:
        return EgressHooks(request=[self.request], response=[self.response])

    def request(self, head: RequestHead) -> RequestHead | None:
        if head.port not in self.ports:
            return None
        headers = list(head.headers)
        target = head.target
        used: list[Injection] = []
        for injection in self.injections:
            if not injection.applies(head.host):
                continue
            hit = False
            for index, (name, value) in enumerate(headers):
                new = value.replace(injection.placeholder, injection.value)
                if name.lower() == "authorization":
                    basic = _basic(new, injection)
                    if basic is not None:
                        new = basic
                if new != value:
                    headers[index] = (name, new)
                    hit = True
            query = _query(target, injection)
            if query is not None:
                target = query
                hit = True
            if hit:
                used.append(injection)
        if not used:
            return None
        for injection in used:
            self._record(injection, head.host)
        return RequestHead(
            method=head.method,
            target=target,
            headers=tuple(headers),
            host=head.host,
            port=head.port,
        )

    def response(
        self, head: RequestHead, response: ResponseHead
    ) -> ResponseHead | None:
        if head.port not in self.ports:
            return None
        headers: Headers = response.headers
        changed = False
        for injection in self.injections:
            if not injection.applies(head.host) or not injection.value:
                continue
            masked = tuple(
                (name, value.replace(injection.value, injection.placeholder))
                for name, value in headers
            )
            if masked != headers:
                headers = masked
                changed = True
        if not changed:
            return None
        return ResponseHead(
            status=response.status, reason=response.reason, headers=headers
        )

    def _record(self, injection: Injection, host: str) -> None:
        log_event(
            log,
            logging.INFO,
            "egress injection",
            event="egress.injection",
            session_id=self.session_id,
            credential_id=injection.credential_id,
            host=norm_host(host),
        )
        metrics = self.metrics if self.metrics is not None else egress_metrics()
        if metrics is not None:
            metrics.observe_egress_injection()


def injector_for(
    credentials: Iterable[ContextEnvCredential], *, session_id: str | None = None
) -> SecretInjector:
    return SecretInjector(
        (
            Injection(
                credential_id=item.credential_id,
                secret_name=item.secret_name,
                placeholder=new_placeholder(),
                value=item.secret_value,
                hosts=tuple(norm_host(host) for host in item.allowed_hosts),
                git_username=item.git_username,
            )
            for item in credentials
        ),
        session_id=session_id,
    )
