import base64
import gzip
import logging
import re
import time
import zlib

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
