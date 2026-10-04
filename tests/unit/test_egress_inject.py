import base64
import logging
import re

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
from apipi.worker.egress import EgressHooks, RequestHead, ResponseHead
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


def test_request_replaces_placeholder_in_query_only() -> None:
    head = _head([], target=f"/search?q=x&key={PH}")
    result = _injector().request(head)
    assert result is not None
    assert result.target == "/search?q=x&key=ghp_real"
    path_only = _injector().request(_head([], target=f"/{PH}/x"))
    assert path_only is None


def test_placeholder_to_other_host_stays() -> None:
    injector = _injector()
    assert (
        injector.request(_head([("Authorization", f"Bearer {PH}")], host="evil.test"))
        is None
    )
    other = injector.request(
        _head([("Authorization", f"Bearer {PH}")], host="other.example.com")
    )
    assert other is None
    mixed = injector.request(
        _head(
            [("Authorization", f"Bearer {OTHER_PH}"), ("X-Key", PH)],
            host="other.example.com",
        )
    )
    assert mixed is not None
    assert mixed.header("authorization") == "Bearer other_real"
    assert mixed.header("x-key") == PH


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
    assert config["GIT_CONFIG_COUNT"] == str(2 + 3 * 4)
    assert config["GIT_CONFIG_KEY_2"] == f"credential.https://{GH}.helper"
    assert config["GIT_CONFIG_VALUE_2"] == "/helper /file"
    assert config["GIT_CONFIG_KEY_3"] == f"url.https://{GH}/.insteadOf"
    assert config["GIT_CONFIG_VALUE_3"] == f"git@{GH}:"
    assert config["GIT_CONFIG_VALUE_4"] == f"ssh://git@{GH}/"
    assert injector_for([]).git_config_env("/h") == {}


async def test_gateway_sends_secret_upstream(env: Env) -> None:
    upstream = await env.upstream()
    injector = SecretInjector([Injection("cred", "SECRET", PH, "real-secret", (HOST,))])
    hooks = EgressHooks(request=[injector.request], response=[injector.response])
    gateway = await env.gateway(
        "restricted", port=upstream.port, intercept=(HOST,), hooks=hooks
    )
    reader, writer = await tls_connect(gateway, trust(env.worker_ca))
    client = HttpClient(reader, writer)
    status, _, _ = await client.request(
        "POST",
        f"/v1/items?key={PH}",
        headers=[
            ("Authorization", _basic(f"user:{PH}")),
            ("X-Api-Key", PH),
        ],
        body=f"body keeps {PH}".encode(),
    )
    assert status == 200
    status, _, _ = await client.request(
        "GET", "/again", headers=[("Authorization", f"Bearer {PH}")]
    )
    assert status == 200
    await close(writer)
    first, second = upstream.seen
    assert first.target == "/v1/items?key=real-secret"
    assert ("X-Api-Key", "real-secret") in first.headers
    assert ("Authorization", _basic("user:real-secret")) in first.headers
    assert first.body == f"body keeps {PH}".encode()
    assert ("Authorization", "Bearer real-secret") in second.headers
    assert upstream.connections == 1
