import base64
import binascii
import json
import logging
import re
import secrets
import zlib
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from urllib.parse import quote

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
DECODE_CHUNK = 256 * 1024
DECODE_FREE = 10 * 1024 * 1024
DECODE_RATIO = 100
MAX_BASIC_TOKENS = 64
STRIPPED = frozenset({"upgrade", "connection", "range", "if-range"})

Masks = list[tuple[bytes, bytes]]


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


def secret_forms(value: str) -> list[str]:
    escaped = json.dumps(value)[1:-1]
    forms = [
        value,
        escaped,
        escaped.replace("/", "\\/"),
        quote(value, safe=""),
        quote(value, safe="/"),
        quote(value, safe="!'()*~-_."),
    ]
    return list(dict.fromkeys(forms))


def ordered_masks(pairs: Iterable[tuple[bytes, bytes]]) -> Masks:
    unique: dict[bytes, bytes] = {}
    for needle, replacement in pairs:
        if needle and needle not in unique:
            unique[needle] = replacement
    return sorted(unique.items(), key=lambda pair: len(pair[0]), reverse=True)


def mask_pattern(masks: Masks) -> re.Pattern[bytes]:
    return re.compile(b"|".join(re.escape(needle) for needle, _ in masks))


def _header(headers: Headers, name: str) -> list[str]:
    key = name.lower()
    return [value for item, value in headers if item.lower() == key]


class _Decoder:
    def __init__(self, coding: str) -> None:
        self.coding = coding
        self.inner: zlib._Decompress | None = None
        self.pending = b""

    def _start(self, data: bytes) -> bytes | None:
        if self.coding == "gzip":
            self.inner = zlib.decompressobj(zlib.MAX_WBITS | 16)
            return data
        self.pending += data
        if len(self.pending) < 2:
            return None
        data, self.pending = self.pending, b""
        wrapped = data[0] & 0x0F == 8 and ((data[0] << 8) | data[1]) % 31 == 0
        self.inner = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
        return data

    def decode(self, data: bytes) -> Iterator[bytes]:
        if self.inner is None:
            started = self._start(data)
            if started is None:
                return
            data = started
        inner = self.inner
        assert inner is not None
        while True:
            if inner.eof:
                if not data:
                    return
                if self.coding != "gzip":
                    raise zlib.error("data after the end of the deflate stream")
                inner = self.inner = zlib.decompressobj(zlib.MAX_WBITS | 16)
            out = inner.decompress(data, DECODE_CHUNK)
            if out:
                yield out
            if inner.eof:
                data = inner.unused_data
                continue
            data = inner.unconsumed_tail
            if not data and len(out) < DECODE_CHUNK:
                return

    def finish(self) -> bytes:
        if self.inner is None:
            if self.pending:
                raise zlib.error("truncated compressed body")
            return b""
        tail = self.inner.flush()
        if not self.inner.eof:
            raise zlib.error("truncated compressed body")
        return tail


class SecretMask:
    def __init__(
        self,
        masks: Masks,
        decoder: _Decoder | None = None,
        *,
        limit: tuple[int, int] | None = (DECODE_FREE, DECODE_RATIO),
        on_limit: Callable[[int, int], None] | None = None,
    ) -> None:
        self.masks = ordered_masks(masks)
        self.replacements = dict(self.masks)
        self.pattern = re.compile(
            b"|".join(re.escape(needle) for needle, _ in self.masks)
        )
        self.carry = b""
        self.decoder = decoder
        self.limit = limit
        self.on_limit = on_limit
        self.encoded = 0
        self.decoded = 0

    def _sub(self, match: re.Match[bytes]) -> bytes:
        return self.replacements[match.group(0)]

    def _hold(self, buf: bytes) -> int:
        size = len(buf)
        best = 0
        for needle, _ in self.masks:
            start = max(0, size - len(needle) + 1)
            first = needle[:1]
            index = buf.find(first, start)
            while index != -1 and size - index > best:
                if needle.startswith(buf[index:]):
                    best = size - index
                    break
                index = buf.find(first, index + 1)
        return best

    def _emit(self, data: bytes) -> bytes:
        if not data:
            return b""
        buf = self.carry + data
        cut = len(buf) - self._hold(buf)
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

    def _check(self, piece: bytes) -> None:
        self.decoded += len(piece)
        if self.limit is None:
            return
        free, ratio = self.limit
        if self.decoded > free and self.decoded > ratio * max(self.encoded, 1):
            if self.on_limit is not None:
                self.on_limit(self.encoded, self.decoded)
            raise InterceptError("decode_limit")

    def feed(self, data: bytes) -> Iterator[bytes]:
        if self.decoder is None:
            yield self._emit(data)
            return
        self.encoded += len(data)
        try:
            for piece in self.decoder.decode(data):
                self._check(piece)
                yield self._emit(piece)
        except zlib.error as exc:
            raise InterceptError("content_decode") from exc

    def end(self) -> Iterator[bytes]:
        if self.decoder is not None:
            try:
                tail = self.decoder.finish()
            except zlib.error as exc:
                raise InterceptError("content_truncated") from exc
            self._check(tail)
            yield self._emit(tail)
        rest = self.pattern.sub(self._sub, self.carry)
        self.carry = b""
        yield rest


class HeaderMask:
    def __init__(self, masks: Masks) -> None:
        self.replacements = dict(ordered_masks(masks))
        self.pattern = mask_pattern(ordered_masks(masks))

    def _sub(self, match: re.Match[bytes]) -> bytes:
        return self.replacements[match.group(0)]

    def text(self, value: str) -> str:
        raw = value.encode("latin-1", "replace")
        return self.pattern.sub(self._sub, raw).decode("latin-1")


class SecretInjector:
    def __init__(
        self,
        injections: Iterable[Injection],
        *,
        session_id: str | None = None,
        metrics: Metrics | None = None,
        ports: Iterable[int] = TLS_PORTS,
        decode_limit: tuple[int, int] | None = (DECODE_FREE, DECODE_RATIO),
    ) -> None:
        self.injections = tuple(
            sorted(injections, key=lambda item: len(item.value), reverse=True)
        )
        self.session_id = session_id
        self.metrics = metrics
        self.ports = frozenset(ports)
        self.decode_limit = decode_limit
        self.basic_tokens: OrderedDict[tuple[str, bytes], bytes] = OrderedDict()

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

    def _masks(self, head: RequestHead, applying: list[Injection]) -> Masks:
        pairs: list[tuple[bytes, bytes]] = []
        for injection in applying:
            placeholder = injection.placeholder.encode()
            for form in secret_forms(injection.value):
                pairs.append((form.encode(), placeholder))
        pairs.extend(self._exchange_tokens(head, applying))
        host = norm_host(head.host)
        for (token_host, real), guest in self.basic_tokens.items():
            if token_host == host:
                pairs.append((real, guest))
        return ordered_masks(pairs)

    def _exchange_tokens(
        self, head: RequestHead, applying: list[Injection]
    ) -> list[tuple[bytes, bytes]]:
        pairs: list[tuple[bytes, bytes]] = []
        for name, value in head.headers:
            if name.lower() != "authorization":
                continue
            scheme, _, token = value.strip().partition(" ")
            if scheme.lower() != "basic" or not token.strip():
                continue
            try:
                decoded = base64.b64decode(token.strip(), validate=True)
            except (binascii.Error, ValueError):
                continue
            guest = decoded
            for injection in applying:
                guest = guest.replace(
                    injection.value.encode(), injection.placeholder.encode()
                )
            if guest != decoded:
                pairs.append((token.strip().encode(), base64.b64encode(guest)))
        return pairs

    def _basic(self, value: str, injection: Injection, host: str) -> str | None:
        scheme, _, token = value.strip().partition(" ")
        if scheme.lower() != "basic" or not token.strip():
            return None
        guest = token.strip()
        try:
            decoded = base64.b64decode(guest, validate=True)
        except (binascii.Error, ValueError):
            return None
        placeholder = injection.placeholder.encode()
        if placeholder not in decoded:
            return None
        real = base64.b64encode(decoded.replace(placeholder, injection.value.encode()))
        key = (norm_host(host), real)
        self.basic_tokens[key] = guest.encode()
        self.basic_tokens.move_to_end(key)
        while len(self.basic_tokens) > MAX_BASIC_TOKENS:
            self.basic_tokens.popitem(last=False)
        return f"{scheme} {real.decode('ascii')}"

    def request(self, head: RequestHead) -> RequestHead | None:
        applying = self._applying(head)
        if not applying:
            return None
        headers = [
            (name, value)
            for name, value in head.headers
            if name.lower() != "accept-encoding" and name.lower() not in STRIPPED
        ]
        headers.append(("Accept-Encoding", "identity"))
        used: list[Injection] = []
        for injection in applying:
            hit = False
            for index, (name, value) in enumerate(headers):
                new = value.replace(injection.placeholder, injection.value)
                if name.lower() == "authorization":
                    basic = self._basic(new, injection, head.host)
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
        mask = HeaderMask(self._masks(head, applying))
        headers: Headers = tuple(
            (name, mask.text(value)) for name, value in response.headers
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
        host = norm_host(head.host)

        def on_limit(encoded: int, decoded: int) -> None:
            log_event(
                log,
                logging.WARNING,
                "egress response decode limit",
                event="egress.decode_limit",
                session_id=self.session_id,
                host=host,
                encoded_bytes=encoded,
                decoded_bytes=decoded,
            )

        return (
            ResponseHead(
                status=response.status, reason=response.reason, headers=headers
            ),
            SecretMask(
                self._masks(head, applying),
                decoder,
                limit=self.decode_limit,
                on_limit=on_limit,
            ),
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
