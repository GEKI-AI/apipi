import asyncio
import base64
import contextlib
import gzip
import logging
import re
import time
import zlib
from typing import Any, cast

import pytest
from tests.unit.test_egress_gateway import (
    HOST,
    Env,
    HttpClient,
    close,
    env,
    tls_connect,
    trust,
)

from apipi.common.metrics import Metrics
from apipi.protocol import ContextEnvCredential
from apipi.worker.egress import Reject, RequestHead, ResponseHead
from apipi.worker.egress.inject import (
    Injection,
    SecretInjector,
    injector_for,
    new_placeholder,
)

__all__ = ["env"]

GH = "github.com"
API = "api.github.com"
PH = "apipi-secret-" + "a" * 32
OTHER_PH = "apipi-secret-" + "b" * 32


def _injector(metrics: Metrics | None = None) -> SecretInjector:
    return SecretInjector(
        [
            Injection(
                credential_id="cred_gh",
                secret_name="GITHUB_TOKEN",
                placeholder=PH,
                value="ghp_real",
                hosts=(GH, API),
            ),
            Injection(
                credential_id="cred_other",
                secret_name="OTHER_KEY",
                placeholder=OTHER_PH,
                value="other_real",
                hosts=("other.example.com",),
            ),
        ],
        session_id="sess",
        metrics=metrics,
    )


def _head(
    headers: list[tuple[str, str]], *, host: str = API, target: str = "/user"
) -> RequestHead:
    return RequestHead(
        method="GET",
        target=target,
        headers=(("Host", host), *headers),
        host=host,
        port=443,
    )


def _basic(text: str) -> str:
    return "Basic " + base64.b64encode(text.encode()).decode()


def test_placeholder_shape() -> None:
    first = new_placeholder()
    assert re.fullmatch(r"apipi-secret-[0-9a-f]{32}", first)
    assert first != new_placeholder()


@pytest.mark.parametrize(
    ("header", "sent", "expected"),
    [
        ("Authorization", f"Bearer {PH}", "Bearer ghp_real"),
        ("Authorization", f"token {PH}", "token ghp_real"),
        ("X-Api-Key", PH, "ghp_real"),
        ("Authorization", _basic(f"x-access-token:{PH}"), None),
        ("Authorization", _basic(f"{PH}:x-oauth-basic"), None),
    ],
)
def test_request_replaces_placeholder_in_headers(
    header: str, sent: str, expected: str | None
) -> None:
    metrics = Metrics()
    result = _injector(metrics).request(_head([(header, sent)]))
    assert result is not None
    value = result.header(header)
    assert value is not None
    if expected is not None:
        assert value == expected
    else:
        decoded = base64.b64decode(value.removeprefix("Basic ")).decode()
        assert PH not in decoded
        assert "ghp_real" in decoded
    assert metrics.egress_injections._value.get() == 1


def test_query_and_path_are_never_substituted() -> None:
    result = _injector().request(_head([], target=f"/search?q=x&key={PH}"))
    assert result is not None
    assert result.target == f"/search?q=x&key={PH}"
    path = _injector().request(_head([], target=f"/{PH}/x"))
    assert path is not None
    assert path.target == f"/{PH}/x"


def test_request_forces_identity_encoding() -> None:
    result = _injector().request(
        _head([("Accept-Encoding", "gzip, br"), ("accept-encoding", "deflate")])
    )
    assert result is not None
    assert [v for k, v in result.headers if k.lower() == "accept-encoding"] == [
        "identity"
    ]
    assert _injector().request(_head([], host="evil.test")) is None


def test_headers_mask_longest_secret_first() -> None:
    injector = SecretInjector(
        [
            Injection("short", "A", PH, "abcdefgh", (API,)),
            Injection("long", "B", OTHER_PH, "abcdefgh-longer", (API,)),
        ]
    )
    response = ResponseHead(
        status=200, reason="OK", headers=(("X-Echo", "abcdefgh-longer"),)
    )
    masked = injector.response(_head([]), response)
    assert masked is not None
    assert masked.headers == (("X-Echo", OTHER_PH),)


def test_placeholder_to_other_host_stays() -> None:
    injector = _injector()
    assert (
        injector.request(_head([("Authorization", f"Bearer {PH}")], host="evil.test"))
        is None
    )
    other = injector.request(
        _head([("Authorization", f"Bearer {PH}")], host="other.example.com")
    )
    assert other is not None
    assert other.header("authorization") == f"Bearer {PH}"
    mixed = injector.request(
        _head(
            [("Authorization", f"Bearer {OTHER_PH}"), ("X-Key", PH)],
            host="other.example.com",
        )
    )
    assert mixed is not None
    assert mixed.header("authorization") == "Bearer other_real"
    assert mixed.header("x-key") == PH


def test_plain_http_never_gets_a_secret() -> None:
    head = RequestHead(
        method="GET",
        target=f"/x?k={PH}",
        headers=(("Host", API), ("Authorization", f"Bearer {PH}")),
        host=API,
        port=80,
    )
    injector = _injector()
    assert injector.request(head) is None
    response = ResponseHead(status=200, reason="OK", headers=(("X", "ghp_real"),))
    assert injector.response(head, response) is None


def test_two_credentials_on_one_host() -> None:
    injector = SecretInjector(
        [
            Injection("a", "A_TOKEN", PH, "real-a", (API,)),
            Injection("b", "B_TOKEN", OTHER_PH, "real-b", (API,)),
        ]
    )
    result = injector.request(_head([("X-A", PH), ("X-B", OTHER_PH)]))
    assert result is not None
    assert result.header("x-a") == "real-a"
    assert result.header("x-b") == "real-b"


def test_response_headers_are_masked() -> None:
    injector = _injector()
    response = ResponseHead(
        status=200,
        reason="OK",
        headers=(("X-Echo", "token ghp_real"), ("Content-Type", "text/plain")),
    )
    masked = injector.response(_head([]), response)
    assert masked is not None
    assert masked.headers[0] == ("X-Echo", f"token {PH}")
    assert injector.response(_head([], host="evil.test"), response) is None
    clean = ResponseHead(status=200, reason="OK", headers=(("A", "b"),))
    assert injector.response(_head([]), clean) is None


def test_injection_log_has_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="apipi.egress")
    _injector().request(_head([("Authorization", f"Bearer {PH}")]))
    records = [
        r for r in caplog.records if getattr(r, "event", "") == "egress.injection"
    ]
    assert len(records) == 1
    assert records[0].__dict__["credential_id"] == "cred_gh"
    assert records[0].__dict__["host"] == API
    dumped = str(records[0].__dict__)
    assert "ghp_real" not in dumped
    assert PH not in dumped


def test_injector_for_context_and_git_files() -> None:
    injector = injector_for(
        [
            ContextEnvCredential(
                credential_id="c1",
                secret_name="GITHUB_TOKEN",
                secret_value="ghp_real",
                allowed_hosts=["GitHub.com", "api.github.com"],
            ),
            ContextEnvCredential(
                credential_id="c2",
                secret_name="GITLAB_TOKEN",
                secret_value="glpat",
                allowed_hosts=["gitlab.com"],
            ),
            ContextEnvCredential(
                credential_id="c3",
                secret_name="FORGEJO_TOKEN",
                secret_value="fj",
                allowed_hosts=["git.example.com"],
                git_username="apipi-bot",
            ),
        ]
    )
    assert injector.hosts == (GH, API, "gitlab.com", "git.example.com")
    env = injector.guest_env()
    assert set(env) == {"GITHUB_TOKEN", "GITLAB_TOKEN", "FORGEJO_TOKEN"}
    assert len(set(env.values())) == 3
    lines = injector.git_credentials().decode().splitlines()
    assert lines[0] == f"{GH}\tx-access-token\t{env['GITHUB_TOKEN']}"
    assert lines[2] == f"gitlab.com\toauth2\t{env['GITLAB_TOKEN']}"
    assert lines[3] == f"git.example.com\tapipi-bot\t{env['FORGEJO_TOKEN']}"
    blob = injector.git_credentials().decode() + str(env) + repr(injector.injections)
    for secret in ("ghp_real", "glpat", "fj\t", "'fj'"):
        assert secret not in blob
    config = injector.git_config_env("/helper /file", start=2)
    assert config["GIT_CONFIG_COUNT"] == str(2 + 4 * 4)
    assert config["GIT_CONFIG_KEY_2"] == f"credential.https://{GH}.helper"
    assert config["GIT_CONFIG_VALUE_2"] == "/helper /file"
    assert config["GIT_CONFIG_KEY_3"] == f"credential.https://{GH}:8443.helper"
    assert config["GIT_CONFIG_KEY_4"] == f"url.https://{GH}/.insteadOf"
    assert config["GIT_CONFIG_VALUE_4"] == f"git@{GH}:"
    assert config["GIT_CONFIG_VALUE_5"] == f"ssh://git@{GH}/"
    assert injector_for([]).git_config_env("/h") == {}


def _body(
    injector: SecretInjector, headers: tuple[tuple[str, str], ...], chunks: list[bytes]
) -> tuple[ResponseHead, bytes]:
    result = injector.body(
        _head([]), ResponseHead(status=200, reason="OK", headers=headers)
    )
    assert isinstance(result, tuple)
    head, mask = result
    out = bytearray()
    for chunk in chunks:
        for piece in mask.feed(chunk):
            out.extend(piece)
    for piece in mask.end():
        out.extend(piece)
    return head, bytes(out)


def test_body_masks_secret_split_across_chunks() -> None:
    injector = _injector()
    data = b"a" * 100 + b"ghp_real" + b"b" * 50 + b"other_real" + b"x"
    for size in (1, 3, 7, 8, 9, 64):
        chunks = [data[i : i + size] for i in range(0, len(data), size)]
        _, out = _body(injector, (("Content-Length", str(len(data))),), chunks)
        assert b"ghp_real" not in out
        assert out == b"a" * 100 + PH.encode() + b"b" * 50 + b"other_real" + b"x"


def test_body_masks_longest_secret_first() -> None:
    injector = SecretInjector(
        [
            Injection("short", "A", PH, "abcdefgh", (API,)),
            Injection("long", "B", OTHER_PH, "abcdefgh-longer", (API,)),
        ]
    )
    _, out = _body(injector, (), [b"x abcdefgh-lon", b"ger y abcdefgh z"])
    assert out == f"x {OTHER_PH} y {PH} z".encode()


@pytest.mark.parametrize("coding", ["gzip", "x-gzip", "deflate", "raw-deflate"])
def test_body_decodes_compressed_responses(coding: str) -> None:
    payload = b"token=ghp_real; " * 2000
    if coding in ("gzip", "x-gzip"):
        packed = gzip.compress(payload)
    elif coding == "deflate":
        packed = zlib.compress(payload)
    else:
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        packed = raw.compress(payload) + raw.flush()
    name = "deflate" if coding == "raw-deflate" else coding
    head, out = _body(
        _injector(),
        (("Content-Encoding", name), ("Content-Type", "text/plain")),
        [packed[i : i + 100] for i in range(0, len(packed), 100)],
    )
    assert out == payload.replace(b"ghp_real", PH.encode())
    assert all(k.lower() != "content-encoding" for k, _ in head.headers)


def test_body_rejects_unknown_encoding() -> None:
    result = _injector().body(
        _head([]),
        ResponseHead(status=200, reason="OK", headers=(("Content-Encoding", "br"),)),
    )
    assert isinstance(result, Reject)
    assert result.status == 502
    other = _injector().body(
        _head([], host="evil.test"),
        ResponseHead(status=200, reason="OK", headers=(("Content-Encoding", "br"),)),
    )
    assert other is None


async def test_gateway_sends_secret_upstream_and_masks_responses(env: Env) -> None:
    upstream = await env.upstream()
    secret = "real-secret-value"
    injector = SecretInjector(
        [Injection("cred", "SECRET", PH, secret, (HOST,))],
        ports=(upstream.port,),
    )
    gateway = await env.gateway(
        "restricted", port=upstream.port, intercept=(HOST,), hooks=injector.hooks()
    )
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    client = HttpClient(reader, writer)
    status, headers, body = await client.request(
        "POST",
        f"/v1/items?key={PH}",
        headers=[
            ("Authorization", _basic(f"user:{PH}")),
            ("X-Api-Key", PH),
            ("Accept-Encoding", "gzip"),
        ],
        body=f"body keeps {PH}".encode(),
    )
    assert status == 200
    assert "content-length" not in headers
    status, _, body = await client.request(
        "GET", f"/echo/{secret}", headers=[("Authorization", f"Bearer {PH}")]
    )
    assert status == 200
    assert body == f"hello GET /echo/{PH} 0".encode()
    status, headers, body = await client.request("HEAD", "/head")
    assert status == 200
    await close(writer)
    first, second, _third = upstream.seen
    assert first.target == f"/v1/items?key={PH}"
    assert ("X-Api-Key", secret) in first.headers
    assert ("Authorization", _basic(f"user:{secret}")) in first.headers
    assert ("Accept-Encoding", "identity") in first.headers
    assert first.body == f"body keeps {PH}".encode()
    assert ("Authorization", f"Bearer {secret}") in second.headers
    assert upstream.connections == 1


@pytest.mark.slow
async def test_masked_body_throughput(env: Env) -> None:
    total = 64 * 1024 * 1024
    upstream = await env.upstream()
    injector = SecretInjector(
        [Injection("cred", "SECRET", PH, "real-secret-value", (HOST,))],
        ports=(upstream.port,),
    )
    gateway = await env.gateway(
        "restricted", port=upstream.port, intercept=(HOST,), hooks=injector.hooks()
    )
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    client = HttpClient(reader, writer)
    started = time.perf_counter()
    status, _, body = await client.request("GET", f"/big/{total}")
    wall = time.perf_counter() - started
    await close(writer)
    assert status == 200
    assert len(body) == total
    rate = total * 8 / 1_000_000 / wall
    print(f"\nmasked body throughput over 64 MiB: {rate:.0f} Mbit/s")
    assert rate >= 50


def _mask(masks: list[tuple[bytes, bytes]], chunks: list[bytes]) -> bytes:
    from apipi.worker.egress.inject import SecretMask

    mask = SecretMask(masks)
    out = bytearray()
    for chunk in chunks:
        for piece in mask.feed(chunk):
            out.extend(piece)
    for piece in mask.end():
        out.extend(piece)
    return bytes(out)


def test_mask_holds_back_only_secret_prefixes() -> None:
    from apipi.worker.egress.inject import SecretMask

    mask = SecretMask([(b"s3cr3t-value", b"[ph]")])
    event = b'data: {"delta": "hello"}\n\n'
    assert b"".join(mask.feed(event)) == event
    assert b"".join(mask.feed(b"tail s3cr")) == b"tail "
    assert b"".join(mask.feed(b"3t-value done")) == b"[ph] done"
    assert b"".join(mask.end()) == b""


def test_mask_chunked_equals_one_shot() -> None:
    import random

    masks = [
        (b"abcdefgh", b"<A>"),
        (b"abcdefgh-longer", b"<B>"),
        (b"xyzxyzxy", b"<C>"),
        (b"Basic dXNlcjpzZWNyZXQ=", b"<D>"),
    ]
    rng = random.Random(519)
    alphabet = [b"a", b"b", b"c", b"abcdefgh", b"abcdefgh-longer", b"xyz", b"-", b" "]
    for _ in range(300):
        data = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        expected = _mask(masks, [data])
        cuts = sorted(rng.sample(range(len(data) + 1), min(len(data), 5)))
        chunks = [
            data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)
        ]
        assert _mask(masks, chunks) == expected
        for needle, _ in masks:
            assert needle not in expected


def test_body_masks_escaped_forms_and_basic_tokens() -> None:
    secret = 'ab/cd"ef\\gh+ij'
    injector = SecretInjector([Injection("cred", "TOKEN", PH, secret, (API,))])
    sent = injector.request(_head([("Authorization", _basic(f"bot:{PH}"))]))
    assert sent is not None
    real_basic = base64.b64encode(f"bot:{secret}".encode())
    guest_basic = base64.b64encode(f"bot:{PH}".encode())
    json_form = 'ab\\/cd\\"ef\\\\gh+ij'
    body = (
        f"raw={secret} json={json_form} pct=ab%2Fcd%22ef%5Cgh%2Bij "
        f"pct2=ab/cd%22ef%5Cgh%2Bij basic={real_basic.decode()}"
    ).encode()
    _, out = _body(injector, (), [body])
    assert secret.encode() not in out
    assert real_basic not in out
    assert guest_basic in out
    assert out.count(PH.encode()) == 4
    header = injector.response(
        _head([]),
        ResponseHead(
            status=200,
            reason="OK",
            headers=(("X-Echo", f"Basic {real_basic.decode()}"),),
        ),
    )
    assert header is not None
    assert header.headers == (("X-Echo", f"Basic {guest_basic.decode()}"),)


def test_request_strips_upgrade_for_credential_hosts() -> None:
    result = _injector().request(
        _head([("Connection", "Upgrade"), ("Upgrade", "websocket")])
    )
    assert result is not None
    names = {name.lower() for name, _ in result.headers}
    assert "upgrade" not in names
    assert "connection" not in names


def test_gzip_multi_member_truncation_and_short_deflate() -> None:
    from apipi.worker.egress.intercept import InterceptError

    payload = b"one ghp_real " * 100
    two = gzip.compress(payload) + gzip.compress(payload)
    _, out = _body(_injector(), (("Content-Encoding", "gzip"),), [two])
    assert out == (payload * 2).replace(b"ghp_real", PH.encode())
    packed = gzip.compress(payload * 50)
    with pytest.raises(InterceptError, match="content_truncated"):
        _body(_injector(), (("Content-Encoding", "gzip"),), [packed[:-20]])
    raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    deflated = raw.compress(payload) + raw.flush()
    pieces = [deflated[:1], deflated[1:2], deflated[2:]]
    _, out = _body(_injector(), (("Content-Encoding", "deflate"),), pieces)
    assert out == payload.replace(b"ghp_real", PH.encode())
    wrapped = zlib.compress(payload)
    pieces = [wrapped[:1], wrapped[1:]]
    _, out = _body(_injector(), (("Content-Encoding", "deflate"),), pieces)
    assert out == payload.replace(b"ghp_real", PH.encode())


def test_decode_limit_stops_gzip_bombs(caplog: pytest.LogCaptureFixture) -> None:
    from apipi.worker.egress.intercept import InterceptError

    caplog.set_level(logging.WARNING, logger="apipi.egress")
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))
    with pytest.raises(InterceptError, match="decode_limit"):
        _body(_injector(), (("Content-Encoding", "gzip"),), [bomb])
    assert any(
        getattr(record, "event", "") == "egress.decode_limit"
        for record in caplog.records
    )
    normal = gzip.compress(bytes(range(256)) * 4096)
    _, out = _body(_injector(), (("Content-Encoding", "gzip"),), [normal])
    assert len(out) == 256 * 4096


class Canned:
    def __init__(self, env: Env, response: bytes) -> None:
        self.env = env
        self.response = response
        self.port = 0
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._serve, "127.0.0.1", 0, ssl=self.env.upstream_ca.server_context(HOST)
        )
        self.port = int(self.server.sockets[0].getsockname()[1])

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(Exception):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(self.response)
            await writer.drain()
            await asyncio.sleep(30)
        writer.close()

    def close(self) -> None:
        if self.server is not None:
            self.server.close()


async def _canned_gateway(
    env: Env, response: bytes, *, limit: tuple[int, int] | None = (10 << 20, 100)
) -> tuple[Canned, object]:
    upstream = Canned(env, response)
    await upstream.start()
    injector = SecretInjector(
        [Injection("cred", "SECRET", PH, "real-secret-value", (HOST,))],
        ports=(upstream.port,),
        decode_limit=limit,
    )
    gateway = await env.gateway(
        "restricted", port=upstream.port, intercept=(HOST,), hooks=injector.hooks()
    )
    return upstream, gateway


async def test_gzip_bomb_memory_stays_bounded_with_a_slow_client(env: Env) -> None:
    import tracemalloc

    body = gzip.compress(b"\0" * (256 * 1024 * 1024), compresslevel=9)
    response = (
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    upstream, gateway = await _canned_gateway(env, response, limit=None)
    tracemalloc.start()
    try:
        writers = []
        for _ in range(4):
            reader, writer = await tls_connect(cast(Any, gateway), trust(env.worker_ca))
            writer.write(f"GET /bomb HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            writers.append(writer)
        await asyncio.sleep(1.5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        for writer in writers:
            await close(writer)
        upstream.close()
    print(f"\ngzip bomb peak traced memory: {peak / 1048576:.1f} MiB")
    assert peak < 48 * 1024 * 1024


async def test_gateway_aborts_truncated_gzip_and_drops_trailers(env: Env) -> None:
    payload = b"secret real-secret-value here " * 1000
    packed = gzip.compress(payload)
    truncated = (
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\n"
        + f"Content-Length: {len(packed) - 10}\r\n\r\n".encode()
        + packed[:-10]
    )
    upstream, gateway = await _canned_gateway(env, truncated)
    reader, writer = await tls_connect(cast(Any, gateway), trust(env.worker_ca))
    writer.write(f"GET /t HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    await writer.drain()
    data = b""
    with contextlib.suppress(Exception):
        while chunk := await asyncio.wait_for(reader.read(65536), timeout=5):
            data += chunk
    await close(writer)
    upstream.close()
    assert data.startswith(b"HTTP/1.1 200")
    assert b"real-secret-value" not in data
    assert not data.endswith(b"0\r\n\r\n")
    trailer = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nTrailer: X-Sig\r\n\r\n"
        b"5\r\nhello\r\n0\r\nX-Sig: real-secret-value\r\n\r\n"
    )
    upstream, gateway = await _canned_gateway(env, trailer)
    reader, writer = await tls_connect(cast(Any, gateway), trust(env.worker_ca))
    status, headers, body = await HttpClient(reader, writer).request("GET", "/x")
    await close(writer)
    upstream.close()
    assert status == 200
    assert body == b"hello"
    assert "x-sig" not in headers


async def test_plain_http_to_credential_host_is_rejected(env: Env) -> None:
    gateway = await env.gateway("enabled", port=18080, intercept=(HOST,), http=True)
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(f"GET / HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    await writer.drain()
    reply = await asyncio.wait_for(reader.read(1024), timeout=5)
    await close(writer)
    assert reply.startswith(b"HTTP/1.1 403")


def test_encode_uri_component_form_is_masked() -> None:
    from urllib.parse import quote

    from apipi.worker.egress.inject import secret_forms

    secret = "ab/cd!ef*gh(1)"
    component = quote(secret, safe="!'()*~-_.")
    assert component == "ab%2Fcd!ef*gh(1)"
    assert component in secret_forms(secret)
    injector = SecretInjector([Injection("cred", "TOKEN", PH, secret, (API,))])
    _, out = _body(injector, (), [f"x={component}&y={secret}".encode()])
    assert out == f"x={PH}&y={PH}".encode()


def test_request_strips_range_for_credential_hosts() -> None:
    result = _injector().request(
        _head([("Range", "bytes=0-3"), ("If-Range", '"etag"'), ("Accept", "*/*")])
    )
    assert result is not None
    names = {name.lower() for name, _ in result.headers}
    assert "range" not in names
    assert "if-range" not in names
    assert "accept" in names
    other = _injector().request(_head([("Range", "bytes=0-3")], host="evil.test"))
    assert other is None


def test_header_and_body_masking_agree() -> None:
    injector = SecretInjector(
        [
            Injection("short", "A", PH, "abcdefgh", (API,)),
            Injection("long", "B", OTHER_PH, "abcdefgh-longer", (API,)),
        ]
    )
    text = "1 abcdefgh-longer 2 abcdefgh 3 abcdefgh-lo"
    _, body = _body(injector, (), [text.encode()])
    header = injector.response(
        _head([]), ResponseHead(status=200, reason="OK", headers=(("X", text),))
    )
    assert header is not None
    assert header.headers[0][1].encode() == body


async def test_informational_response_headers_are_masked(env: Env) -> None:
    response = (
        b"HTTP/1.1 103 Early Hints\r\nLink: </a?k=real-secret-value>\r\n\r\n"
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    )
    upstream, gateway = await _canned_gateway(env, response)
    reader, writer = await tls_connect(cast(Any, gateway), trust(env.worker_ca))
    writer.write(f"GET /hints HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    await writer.drain()
    data = b""
    with contextlib.suppress(Exception):
        while b"ok" not in data.split(b"\r\n\r\n")[-1]:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=5)
            if not chunk:
                break
            data += chunk
    await close(writer)
    upstream.close()
    assert data.startswith(b"HTTP/1.1 103")
    assert b"real-secret-value" not in data
    assert PH.encode() in data


def test_basic_token_stays_masked_after_cache_eviction() -> None:
    from apipi.worker.egress.inject import MAX_BASIC_TOKENS

    injector = SecretInjector(
        [Injection("cred", "TOKEN", PH, "real-secret-value", (API,))]
    )
    sent = injector.request(_head([("Authorization", _basic(f"alice:{PH}"))]))
    assert sent is not None
    for index in range(MAX_BASIC_TOKENS + 5):
        injector.request(_head([("Authorization", _basic(f"user{index}:{PH}"))]))
    real = base64.b64encode(b"alice:real-secret-value").decode()
    guest = base64.b64encode(f"alice:{PH}".encode()).decode()
    assert all(b"alice" not in base64.b64decode(r) for _, r in injector.basic_tokens)
    echoed = ResponseHead(
        status=200, reason="OK", headers=(("X-Echo", f"Basic {real}"),)
    )
    masked = injector.response(sent, echoed)
    assert masked is not None
    assert masked.headers == (("X-Echo", f"Basic {guest}"),)
    result = injector.body(sent, ResponseHead(status=200, reason="OK", headers=()))
    assert isinstance(result, tuple)
    _, mask = result
    out = b"".join([*mask.feed(f"token {real} end".encode()), *mask.end()])
    assert out == f"token {guest} end".encode()
