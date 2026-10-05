"""Transport security for the split worker socket (#450)."""

import ipaddress
import ssl
from pathlib import Path
from typing import Any

import pytest
import websockets
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from websockets.asyncio.server import ServerConnection, serve

from apipi.config import ConfigError, Settings
from apipi.worker.client import _worker_connect_kwargs
from apipi.worker.tls import (
    check_worker_mtls_files,
    is_loopback_host,
    require_worker_tls,
    worker_ssl_context,
)


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "127.0.0.1", "127.0.0.2", "::1", " 127.0.0.1 "],
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["api.example", "10.0.0.1", "192.168.1.5", "example.com", "", None],
)
def test_non_loopback_hosts(host: str | None) -> None:
    assert is_loopback_host(host) is False


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "ws://127.0.0.1:8000",
        "ws://[::1]:8000",
    ],
)
def test_loopback_plain_url_allowed(base: str) -> None:
    url = require_worker_tls(base)
    assert url.startswith("ws://")
    assert url.endswith("/internal/worker")


@pytest.mark.parametrize(
    "base",
    [
        "https://api.example:8000",
        "wss://api.example/internal/worker",
    ],
)
def test_tls_url_allowed(base: str) -> None:
    url = require_worker_tls(base)
    assert url.startswith("wss://")


@pytest.mark.parametrize(
    "base",
    [
        "http://api.example:8000",
        "ws://api.example/internal/worker",
        "http://10.0.0.1:8000",
        "http://192.168.1.5:8000",
    ],
)
def test_non_loopback_plain_url_rejected(base: str) -> None:
    with pytest.raises(ConfigError, match="must use TLS"):
        require_worker_tls(base)


def test_mtls_files_empty_ok(tmp_path: Path) -> None:
    settings = Settings(run_mode="none")
    check_worker_mtls_files(settings)
    assert worker_ssl_context(settings) is None


def test_mtls_pair_required(tmp_path: Path) -> None:
    cert = tmp_path / "client.crt"
    cert.write_text("cert")
    settings = Settings(run_mode="none", worker_client_cert=str(cert))
    with pytest.raises(ConfigError, match="must be set together"):
        check_worker_mtls_files(settings)
    with pytest.raises(ConfigError, match="must be set together"):
        worker_ssl_context(settings)


def test_mtls_missing_file_rejected(tmp_path: Path) -> None:
    settings = Settings(
        run_mode="none",
        worker_client_cert=str(tmp_path / "missing.crt"),
        worker_client_key=str(tmp_path / "missing.key"),
        worker_server_ca=str(tmp_path / "missing-ca.crt"),
    )
    with pytest.raises(ConfigError, match="not a readable file"):
        check_worker_mtls_files(settings)


def _make_ca(tmp_path: Path) -> tuple[Path, RSAPrivateKey, x509.Certificate]:
    """Generate a self-signed CA; return (ca_path, key, cert)."""
    import datetime

    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.crt"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return ca, key, cert


def _issue_cert(
    tmp_path: Path,
    name: str,
    ca_key: RSAPrivateKey,
    ca_cert: x509.Certificate,
    sans: list[x509.GeneralName] | None = None,
) -> tuple[Path, Path]:
    """Issue a CA-signed cert; return (cert_path, key_path)."""
    import datetime

    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(sans), critical=False
        )
    builder = builder.add_extension(
        x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
        False,
    )
    cert = builder.sign(ca_key, hashes.SHA256())
    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_mtls_server_ca_only_builds_context(tmp_path: Path) -> None:
    ca, _, _ = _make_ca(tmp_path)
    settings = Settings(run_mode="none", worker_server_ca=str(ca))
    check_worker_mtls_files(settings)
    context = worker_ssl_context(settings)
    assert isinstance(context, ssl.SSLContext)


def test_invalid_tls_material_is_config_error(tmp_path: Path) -> None:
    bad_ca = tmp_path / "bad-ca.crt"
    bad_ca.write_text("not a pem bundle")
    settings = Settings(run_mode="none", worker_server_ca=str(bad_ca))
    check_worker_mtls_files(settings)
    with pytest.raises(ConfigError, match="APIPI_WORKER_SERVER_CA"):
        worker_ssl_context(settings)


def test_mismatched_key_is_config_error(tmp_path: Path) -> None:
    _, ca_key, ca_cert = _make_ca(tmp_path)
    cert_path, _ = _issue_cert(tmp_path, "client", ca_key, ca_cert)
    _, other_key = _issue_cert(tmp_path, "other", ca_key, ca_cert)
    settings = Settings(
        run_mode="none",
        worker_client_cert=str(cert_path),
        worker_client_key=str(other_key),
    )
    check_worker_mtls_files(settings)
    with pytest.raises(ConfigError, match="APIPI_WORKER_CLIENT_CERT"):
        worker_ssl_context(settings)


def test_connect_kwargs_omit_ssl_without_material() -> None:
    settings = Settings(run_mode="none")
    assert _worker_connect_kwargs(settings, "wss://api.example/internal/worker") == {}
    assert _worker_connect_kwargs(settings, "ws://127.0.0.1:8000/internal/worker") == {}


def test_connect_kwargs_pass_ssl_with_material(tmp_path: Path) -> None:
    ca, _, _ = _make_ca(tmp_path)
    settings = Settings(run_mode="none", worker_server_ca=str(ca))
    kwargs = _worker_connect_kwargs(settings, "wss://api.example/internal/worker")
    assert isinstance(kwargs.get("ssl"), ssl.SSLContext)


def _server_context(
    cert_path: Path,
    key_path: Path,
    ca_path: Path | None = None,
) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    if ca_path is not None:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(cafile=str(ca_path))
    return context


async def _echo(sock: ServerConnection) -> None:
    message = await sock.recv()
    await sock.send(message)


async def _roundtrip(url: str, kwargs: dict[str, Any]) -> None:
    async with websockets.connect(url, **kwargs) as sock:
        await sock.send("hello")
        assert await sock.recv() == "hello"


def _loopback_san() -> list[x509.GeneralName]:
    return [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]


async def test_wss_handshake_with_server_ca(tmp_path: Path) -> None:
    ca, ca_key, ca_cert = _make_ca(tmp_path)
    cert_path, key_path = _issue_cert(
        tmp_path, "server", ca_key, ca_cert, _loopback_san()
    )
    settings = Settings(run_mode="none", worker_server_ca=str(ca))

    async with serve(
        _echo,
        "127.0.0.1",
        0,
        ssl=_server_context(cert_path, key_path),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"wss://127.0.0.1:{port}/internal/worker"
        await _roundtrip(url, _worker_connect_kwargs(settings, url))


async def test_wss_handshake_with_mtls_pair(tmp_path: Path) -> None:
    ca, ca_key, ca_cert = _make_ca(tmp_path)
    server_cert, server_key = _issue_cert(
        tmp_path, "server", ca_key, ca_cert, _loopback_san()
    )
    client_cert, client_key = _issue_cert(tmp_path, "client", ca_key, ca_cert)
    settings = Settings(
        run_mode="none",
        worker_client_cert=str(client_cert),
        worker_client_key=str(client_key),
        worker_server_ca=str(ca),
    )

    async with serve(
        _echo,
        "127.0.0.1",
        0,
        ssl=_server_context(server_cert, server_key, ca),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"wss://127.0.0.1:{port}/internal/worker"
        await _roundtrip(url, _worker_connect_kwargs(settings, url))


async def test_wss_handshake_mtls_pair_required(tmp_path: Path) -> None:
    ca, ca_key, ca_cert = _make_ca(tmp_path)
    server_cert, server_key = _issue_cert(
        tmp_path, "server", ca_key, ca_cert, _loopback_san()
    )

    async with serve(
        _echo,
        "127.0.0.1",
        0,
        ssl=_server_context(server_cert, server_key, ca),
    ) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"wss://127.0.0.1:{port}/internal/worker"
        bare = ssl.create_default_context(cafile=str(ca))
        # Without a client certificate the server aborts the TLS
        # handshake, which surfaces as a failed HTTP upgrade.
        with pytest.raises(websockets.exceptions.InvalidMessage):
            async with websockets.connect(url, ssl=bare):
                pass
