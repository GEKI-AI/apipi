import builtins
import contextlib
import datetime
import io
import os
import pathlib
import ssl
import tempfile
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from apipi.worker.egress import ca as ca_module
from apipi.worker.egress.ca import WorkerCA, worker_ca


def _handshake(server: ssl.SSLContext, client: ssl.SSLContext, host: str) -> str:
    c_in, c_out, s_in, s_out = (ssl.MemoryBIO() for _ in range(4))
    tls_client = client.wrap_bio(c_in, c_out, server_hostname=host)
    tls_server = server.wrap_bio(s_in, s_out, server_side=True)
    for _ in range(10):
        for side in (tls_client, tls_server):
            with contextlib.suppress(ssl.SSLWantReadError):
                side.do_handshake()
        s_in.write(c_out.read())
        c_in.write(s_out.read())
        try:
            tls_client.do_handshake()
            tls_server.do_handshake()
        except ssl.SSLWantReadError:
            continue
        break
    alpn = tls_client.selected_alpn_protocol()
    return alpn or ""


def test_ca_certificate_shape() -> None:
    ca = WorkerCA()
    cert = x509.load_pem_x509_certificate(ca.cert_pem)
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.critical
    assert constraints.value.ca is True
    assert constraints.value.path_length == 0
    key = cert.public_key()
    assert isinstance(key, ec.EllipticCurvePublicKey)
    assert key.curve.name == "secp256r1"
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert lifetime <= datetime.timedelta(days=366)
    assert b"PRIVATE KEY" not in ca.cert_pem


def test_leaf_verifies_against_ca_and_offers_http1_only() -> None:
    ca = WorkerCA()
    client = ssl.create_default_context(cadata=ca.cert_pem.decode())
    client.set_alpn_protocols(["h2", "http/1.1"])
    assert _handshake(
        ca.server_context("api.example.com"), client, "api.example.com"
    ) == ("http/1.1")
    other = ssl.create_default_context(cadata=ca.cert_pem.decode())
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(ca.server_context("api.example.com"), other, "evil.example.com")
    stranger = ssl.create_default_context(cadata=WorkerCA().cert_pem.decode())
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(ca.server_context("api.example.com"), stranger, "api.example.com")


def test_leaf_cache_is_lru() -> None:
    ca = WorkerCA(cache_size=2)
    first = ca.server_context("a.example")
    assert ca.server_context("a.example") is first
    ca.server_context("b.example")
    ca.server_context("a.example")
    ca.server_context("c.example")
    assert ca.server_context("a.example") is first
    assert len(ca._leaves) == 2
    assert "b.example" not in ca._leaves


def test_ca_key_never_touches_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[Any] = []
    real_open = builtins.open
    real_os_open = os.open

    def track_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(file)
        return real_open(file, *args, **kwargs)

    def track_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(path)
        return real_os_open(path, *args, **kwargs)

    def deny(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("egress CA must not write files")

    monkeypatch.setattr(builtins, "open", track_open)
    monkeypatch.setattr(io, "open", track_open)
    monkeypatch.setattr(os, "open", track_os_open)
    monkeypatch.setattr(pathlib.Path, "write_bytes", deny)
    monkeypatch.setattr(pathlib.Path, "write_text", deny)
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", deny)
    monkeypatch.setattr(tempfile, "mkstemp", deny)
    memfds: list[str] = []
    real_memfd = ca_module.memfd

    def track_memfd(name: str) -> int:
        memfds.append(name)
        fd = real_memfd(name)
        assert os.readlink(f"/proc/self/fd/{fd}").startswith("/memfd:")
        return fd

    monkeypatch.setattr(ca_module, "memfd", track_memfd)
    ca = WorkerCA()
    ca.server_context("api.example.com")
    assert opened == []
    assert memfds == ["apipi-egress-leaf"]
    assert not hasattr(ca, "key_pem")
    for name in dir(ca):
        value = getattr(ca, name)
        if isinstance(value, bytes):
            assert b"PRIVATE KEY" not in value


def test_worker_ca_is_shared_for_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca_module, "_current", None)
    first = worker_ca()
    assert worker_ca() is first
    lifetime = first.not_after - datetime.datetime.now(datetime.UTC)
    assert datetime.timedelta(days=360) < lifetime <= datetime.timedelta(days=366)
