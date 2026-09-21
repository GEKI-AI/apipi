import base64
import hashlib
import os
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_KEY_VERSION = "v1"
VAULT_CIPHER_PREFIX = f"{VAULT_KEY_VERSION}:"
VAULT_NONCE_LEN = 12
VAULT_KEY_LEN = 32
DEV_VAULT_MASTER_KEY = hashlib.sha256(b"apipi.dev.vault.master.key.v1").digest()
VAULT_MASTER_KEY_HELP = "APIPI_VAULT_MASTER_KEY must be 32 bytes (base64 or hex)"


class VaultCryptoError(Exception):
    pass


def vault_master_key_unset(value: str | None) -> bool:
    return value is None or value.strip() == ""


def parse_vault_master_key(value: str) -> bytes:
    text = value.strip()
    if len(text) == 64 and all(char in "0123456789abcdefABCDEF" for char in text):
        key = bytes.fromhex(text)
    else:
        pad = "=" * ((4 - len(text) % 4) % 4)
        raw = (text + pad).encode("ascii")
        try:
            key = base64.urlsafe_b64decode(raw)
        except (ValueError, OSError):
            try:
                key = base64.b64decode(raw)
            except (ValueError, OSError) as exc:
                raise ValueError(VAULT_MASTER_KEY_HELP) from exc
    if len(key) != VAULT_KEY_LEN:
        raise ValueError(VAULT_MASTER_KEY_HELP)
    return key


def vault_key_bytes(value: str | None) -> bytes:
    if vault_master_key_unset(value):
        return DEV_VAULT_MASTER_KEY
    return parse_vault_master_key(value or "")


def vault_aad(tenant_id: uuid.UUID, credential_id: uuid.UUID) -> bytes:
    return f"{tenant_id}:{credential_id}".encode()


def is_vault_ciphertext(stored: str) -> bool:
    return stored.startswith(VAULT_CIPHER_PREFIX)


def encrypt_vault_token(plaintext: str, key: bytes, *, aad: bytes) -> str:
    nonce = os.urandom(VAULT_NONCE_LEN)
    blob = nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), aad)
    packed = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    return f"{VAULT_CIPHER_PREFIX}{packed}"


def decrypt_vault_token(stored: str, key: bytes, *, aad: bytes) -> str:
    if not is_vault_ciphertext(stored):
        return stored
    packed = stored[len(VAULT_CIPHER_PREFIX) :]
    pad = "=" * ((4 - len(packed) % 4) % 4)
    try:
        blob = base64.urlsafe_b64decode(packed + pad)
    except (ValueError, OSError) as exc:
        raise VaultCryptoError("vault credential decrypt failed") from exc
    if len(blob) < VAULT_NONCE_LEN + 16:
        raise VaultCryptoError("vault credential decrypt failed")
    nonce, body = blob[:VAULT_NONCE_LEN], blob[VAULT_NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, body, aad).decode()
    except Exception as exc:
        raise VaultCryptoError("vault credential decrypt failed") from exc
