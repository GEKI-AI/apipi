import ctypes
import datetime
import os
import ssl
import threading
from collections import OrderedDict

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_VALIDITY = datetime.timedelta(days=365)
LEAF_CACHE_SIZE = 1024
CA_NAME = "ApiPi worker egress CA"
INTERCEPT_ALPN = ("http/1.1",)
MFD_CLOEXEC = 1


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _key_id(key: ec.EllipticCurvePrivateKey) -> x509.SubjectKeyIdentifier:
    return x509.SubjectKeyIdentifier.from_public_key(key.public_key())


def memfd(name: str) -> int:
    create = getattr(os, "memfd_create", None)
    if create is not None:
        return create(name, MFD_CLOEXEC)
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.memfd_create
    call.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    fd = call(name.encode(), MFD_CLOEXEC)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return fd


def _context_from_pem(pem: bytes) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.set_alpn_protocols(list(INTERCEPT_ALPN))
    fd = memfd("apipi-egress-leaf")
    try:
        os.write(fd, pem)
        context.load_cert_chain(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)
    return context


class WorkerCA:
    def __init__(
        self,
        *,
        validity: datetime.timedelta = CA_VALIDITY,
        cache_size: int = LEAF_CACHE_SIZE,
    ) -> None:
        now = _now()
        self._key = ec.generate_private_key(ec.SECP256R1())
        self._name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
        self.not_after = now + validity
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(self._name)
            .issuer_name(self._name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(self.not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
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
            .add_extension(_key_id(self._key), False)
            .sign(self._key, hashes.SHA256())
        )
        self.cert_pem = self.cert.public_bytes(serialization.Encoding.PEM)
        self._cache_size = cache_size
        self._leaves: OrderedDict[str, ssl.SSLContext] = OrderedDict()
        self._lock = threading.Lock()

    def leaf_pem(self, host: str) -> bytes:
        now = _now()
        key = ec.generate_private_key(ec.SECP256R1())
        ca_key_id = _key_id(self._key)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(self.not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False
            )
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
            .add_extension(_key_id(key), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                    ca_key_id
                ),
                False,
            )
            .sign(self._key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return cert.public_bytes(serialization.Encoding.PEM) + key_pem

    def server_context(self, host: str) -> ssl.SSLContext:
        with self._lock:
            cached = self._leaves.get(host)
            if cached is not None:
                self._leaves.move_to_end(host)
                return cached
        context = _context_from_pem(self.leaf_pem(host))
        with self._lock:
            self._leaves[host] = context
            self._leaves.move_to_end(host)
            while len(self._leaves) > self._cache_size:
                self._leaves.popitem(last=False)
        return context


_current: WorkerCA | None = None
_current_lock = threading.Lock()


def worker_ca() -> WorkerCA:
    global _current
    with _current_lock:
        if _current is None:
            _current = WorkerCA()
        return _current
