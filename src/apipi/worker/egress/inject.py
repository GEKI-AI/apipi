import base64
import binascii
import logging
import re
import secrets
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from apipi.common.logutil import log_event
from apipi.common.metrics import Metrics
from apipi.protocol import ContextEnvCredential
from apipi.worker.egress.gateway import TLS_PORTS, egress_metrics
from apipi.worker.egress.intercept import (
    BodyFilter,
    EgressHooks,
    Headers,
    InterceptError,
    Reject,
    RequestHead,
    ResponseHead,
)
from apipi.worker.egress.policy import norm_host

log = logging.getLogger("apipi.egress")

PLACEHOLDER_PREFIX = "apipi-secret-"
GIT_USERNAME_DEFAULTS = {"gitlab.com": "oauth2"}
GIT_USERNAME_FALLBACK = "x-access-token"
GIT_PORTS = (443, 8443)
DECODE_CHUNK = 1 << 20


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


def _header(headers: Headers, name: str) -> list[str]:
    key = name.lower()
    return [value for item, value in headers if item.lower() == key]


class _Decoder:
    def __init__(self, coding: str) -> None:
        self.coding = coding
        wbits = zlib.MAX_WBITS | 16 if coding == "gzip" else zlib.MAX_WBITS
        self.inner = zlib.decompressobj(wbits)
        self.started = False

    def _switch_raw(self, data: bytes) -> bytes:
        self.inner = zlib.decompressobj(-zlib.MAX_WBITS)
        return self.inner.decompress(data, DECODE_CHUNK)

    def decode(self, data: bytes) -> Iterator[bytes]:
        try:
            out = self.inner.decompress(data, DECODE_CHUNK)
        except zlib.error:
            if self.coding != "deflate" or self.started:
                raise
            out = self._switch_raw(data)
        self.started = True
        yield out
        while self.inner.unconsumed_tail:
            yield self.inner.decompress(self.inner.unconsumed_tail, DECODE_CHUNK)

    def flush(self) -> bytes:
        return self.inner.flush()


class SecretMask:
    def __init__(
        self, pairs: list[tuple[bytes, bytes]], decoder: _Decoder | None = None
    ) -> None:
        ordered = sorted(pairs, key=lambda pair: len(pair[0]), reverse=True)
        self.replacements = dict(ordered)
        self.pattern = re.compile(b"|".join(re.escape(value) for value, _ in ordered))
        self.keep = max(len(value) for value, _ in ordered) - 1
        self.carry = b""
        self.decoder = decoder

    def _sub(self, match: re.Match[bytes]) -> bytes:
        return self.replacements[match.group(0)]

    def _emit(self, data: bytes) -> bytes:
        if not data:
            return b""
        buf = self.carry + data
        cut = len(buf) - self.keep
        if cut <= 0:
            self.carry = buf
            return b""
        out: list[bytes] = []
        pos = 0
        for match in self.pattern.finditer(buf):
            if match.start() >= cut:
                break
            out.append(buf[pos : match.start()])
            out.append(self.replacements[match.group(0)])
            pos = match.end()
        end = max(pos, cut)
        out.append(buf[pos:end])
        self.carry = buf[end:]
        return b"".join(out)

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self.decoder is None:
            yield self._emit(data)
            return
        try:
            for piece in self.decoder.decode(data):
                yield self._emit(piece)
        except zlib.error as exc:
            raise InterceptError("content_decode") from exc

    def end(self) -> Iterator[bytes]:
        if self.decoder is not None:
            try:
                yield self._emit(self.decoder.flush())
            except zlib.error as exc:
                raise InterceptError("content_decode") from exc
        tail = self.pattern.sub(self._sub, self.carry)
        self.carry = b""
        yield tail


class SecretInjector:
    def __init__(
        self,
        injections: Iterable[Injection],
        *,
        session_id: str | None = None,
        metrics: Metrics | None = None,
        ports: Iterable[int] = TLS_PORTS,
    ) -> None:
        self.injections = tuple(
            sorted(injections, key=lambda item: len(item.value), reverse=True)
        )
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
            for port in GIT_PORTS:
                origin = host if port == 443 else f"{host}:{port}"
                entries.append((f"credential.https://{origin}.helper", helper))
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

    def hooks(self) -> EgressHooks:
        return EgressHooks(
            request=[self.request], response=[self.response], body=[self.body]
        )

    def _applying(self, head: RequestHead) -> list[Injection]:
        if head.port not in self.ports:
            return []
        return [item for item in self.injections if item.applies(head.host)]

    def request(self, head: RequestHead) -> RequestHead | None:
        applying = self._applying(head)
        if not applying:
            return None
        headers = [
            (name, value)
            for name, value in head.headers
            if name.lower() != "accept-encoding"
        ]
        headers.append(("Accept-Encoding", "identity"))
        used: list[Injection] = []
        for injection in applying:
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
            if hit:
                used.append(injection)
        for injection in used:
            self._record(injection, head.host)
        return RequestHead(
            method=head.method,
            target=head.target,
            headers=tuple(headers),
            host=head.host,
            port=head.port,
        )

    def response(
        self, head: RequestHead, response: ResponseHead
    ) -> ResponseHead | None:
        applying = self._applying(head)
        if not applying:
            return None
        headers: Headers = response.headers
        for injection in applying:
            headers = tuple(
                (name, value.replace(injection.value, injection.placeholder))
                for name, value in headers
            )
        if headers == response.headers:
            return None
        return ResponseHead(
            status=response.status, reason=response.reason, headers=headers
        )

    def body(
        self, head: RequestHead, response: ResponseHead
    ) -> tuple[ResponseHead, BodyFilter] | Reject | None:
        applying = self._applying(head)
        if not applying:
            return None
        codings = [
            part.strip().lower()
            for value in _header(response.headers, "content-encoding")
            for part in value.split(",")
            if part.strip() and part.strip().lower() != "identity"
        ]
        decoder: _Decoder | None = None
        if codings in (["gzip"], ["x-gzip"]):
            decoder = _Decoder("gzip")
        elif codings == ["deflate"]:
            decoder = _Decoder("deflate")
        elif codings:
            log_event(
                log,
                logging.WARNING,
                "egress response encoding not supported",
                event="egress.encoding_rejected",
                session_id=self.session_id,
                host=norm_host(head.host),
                encoding=",".join(codings),
            )
            return Reject(status=502, reason="content_encoding")
        headers = response.headers
        if decoder is not None:
            headers = tuple(
                (name, value)
                for name, value in headers
                if name.lower() != "content-encoding"
            )
        pairs = [(item.value.encode(), item.placeholder.encode()) for item in applying]
        return (
            ResponseHead(
                status=response.status, reason=response.reason, headers=headers
            ),
            SecretMask(pairs, decoder),
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
