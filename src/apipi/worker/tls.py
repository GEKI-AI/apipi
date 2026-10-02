"""Transport security for the split worker socket (#450).

A split worker dials one outbound WebSocket to `/internal/worker`.
Bearer tokens travel on that socket, so TLS is required unless the
API URL is loopback (local development). mTLS is optional: the
worker can present a client certificate, and it can verify the API
server against a private CA bundle instead of the system trust store.
"""

import ipaddress
import ssl
from pathlib import Path
from urllib.parse import urlparse

from apipi.config import ConfigError, Settings

TLS_REQUIRED_MESSAGE = (
    "Worker API URL must use TLS (https:// or wss://) for non-loopback hosts: "
    "{url}. Loopback http:// URLs are allowed for local development only."
)

MTLS_PAIR_MESSAGE = (
    "APIPI_WORKER_CLIENT_CERT and APIPI_WORKER_CLIENT_KEY must be set together."
)


def is_loopback_host(host: str | None) -> bool:
    """Whether `host` is a loopback name or address."""
    if host is None:
        return False
    lowered = host.strip().lower().rstrip(".")
    if lowered in {"localhost"}:
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def require_worker_tls(api_url: str) -> str:
    """Fail startup when a non-loopback worker URL is not TLS.

    Returns the worker WebSocket URL (raising the same error for a
    non-loopback plain URL). Loopback `http://`/`ws://` URLs stay
    allowed for local development.
    """
    from apipi.worker.hub import worker_ws_url

    ws_url = worker_ws_url(api_url)
    parsed = urlparse(ws_url)
    if parsed.scheme == "wss":
        return ws_url
    if parsed.scheme == "ws" and is_loopback_host(parsed.hostname):
        return ws_url
    raise ConfigError(TLS_REQUIRED_MESSAGE.format(url=api_url))


def check_worker_mtls_files(settings: Settings) -> None:
    """Fail startup on a half-configured or unreadable mTLS setup."""
    cert = (settings.worker_client_cert or "").strip()
    key = (settings.worker_client_key or "").strip()
    if bool(cert) != bool(key):
        raise ConfigError(MTLS_PAIR_MESSAGE)
    for label, value in (
        ("APIPI_WORKER_CLIENT_CERT", cert),
        ("APIPI_WORKER_CLIENT_KEY", key),
        ("APIPI_WORKER_SERVER_CA", (settings.worker_server_ca or "").strip()),
    ):
        if not value:
            continue
        path = Path(value)
        if not path.is_file():
            raise ConfigError(f"{label} is not a readable file: {value}")
        try:
            with open(path, "rb"):
                pass
        except OSError:
            raise ConfigError(f"{label} is not a readable file: {value}") from None


def worker_ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """Build the TLS context for a `wss://` worker socket.

    Returns None when no custom TLS material is configured, so the
    WebSocket client verifies the API server against the system trust
    store. Plain `ws://` (loopback only) needs no context."""
    cert = (settings.worker_client_cert or "").strip()
    key = (settings.worker_client_key or "").strip()
    ca = (settings.worker_server_ca or "").strip()
    if not cert and not key and not ca:
        return None
    if bool(cert) != bool(key):
        raise ConfigError(MTLS_PAIR_MESSAGE)
    try:
        context = ssl.create_default_context(cafile=ca or None)
    except ssl.SSLError as exc:
        if ca:
            raise ConfigError(
                f"APIPI_WORKER_SERVER_CA is not a valid CA bundle: {ca}: {exc}"
            ) from exc
        raise
    if cert:
        try:
            context.load_cert_chain(certfile=cert, keyfile=key)
        except ssl.SSLError as exc:
            raise ConfigError(
                "APIPI_WORKER_CLIENT_CERT and APIPI_WORKER_CLIENT_KEY "
                f"are not a valid certificate/key pair: {cert}: {exc}"
            ) from exc
    return context
