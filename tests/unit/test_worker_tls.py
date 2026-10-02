"""Transport security for the split worker socket (#450)."""

from pathlib import Path

import pytest

from apipi.config import ConfigError, Settings
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


def test_mtls_server_ca_only_builds_context(tmp_path: Path) -> None:
    import datetime
    import ssl

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
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
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    ca = tmp_path / "ca.crt"
    ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    settings = Settings(run_mode="none", worker_server_ca=str(ca))
    check_worker_mtls_files(settings)
    context = worker_ssl_context(settings)
    assert isinstance(context, ssl.SSLContext)
