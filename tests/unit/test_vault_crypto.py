import base64
import uuid

import pytest

from apipi.services.vault_crypto import (
    DEV_VAULT_MASTER_KEY,
    VAULT_CIPHER_PREFIX,
    VaultCryptoError,
    decrypt_vault_token,
    encrypt_vault_token,
    is_vault_ciphertext,
    parse_vault_master_key,
    vault_aad,
    vault_key_bytes,
    vault_master_key_unset,
)


def test_round_trip() -> None:
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    aad = vault_aad(tenant_id, cred_id)
    stored = encrypt_vault_token("secret-token", DEV_VAULT_MASTER_KEY, aad=aad)
    assert is_vault_ciphertext(stored)
    assert stored.startswith(VAULT_CIPHER_PREFIX)
    assert "secret-token" not in stored
    assert decrypt_vault_token(stored, DEV_VAULT_MASTER_KEY, aad=aad) == "secret-token"


def test_wrong_key_fails() -> None:
    tenant_id = uuid.uuid4()
    cred_id = uuid.uuid4()
    aad = vault_aad(tenant_id, cred_id)
    stored = encrypt_vault_token("secret-token", DEV_VAULT_MASTER_KEY, aad=aad)
    other = bytes(range(32))
    with pytest.raises(VaultCryptoError, match="decrypt failed"):
        decrypt_vault_token(stored, other, aad=aad)


def test_aad_mismatch_fails() -> None:
    stored = encrypt_vault_token(
        "secret-token",
        DEV_VAULT_MASTER_KEY,
        aad=vault_aad(uuid.uuid4(), uuid.uuid4()),
    )
    with pytest.raises(VaultCryptoError, match="decrypt failed"):
        decrypt_vault_token(
            stored,
            DEV_VAULT_MASTER_KEY,
            aad=vault_aad(uuid.uuid4(), uuid.uuid4()),
        )


def test_legacy_plaintext_passthrough() -> None:
    assert decrypt_vault_token("keep-me", DEV_VAULT_MASTER_KEY, aad=b"x") == "keep-me"


def test_decrypt_error_omits_secrets() -> None:
    stored = encrypt_vault_token("secret-token", DEV_VAULT_MASTER_KEY, aad=b"aad")
    with pytest.raises(VaultCryptoError) as exc:
        decrypt_vault_token(stored, bytes(range(32)), aad=b"aad")
    assert "secret-token" not in str(exc.value)
    assert stored not in str(exc.value)


def test_parse_key_hex_and_base64() -> None:
    key = bytes(range(32))
    assert parse_vault_master_key(key.hex()) == key
    assert parse_vault_master_key(base64.b64encode(key).decode()) == key
    assert parse_vault_master_key(base64.urlsafe_b64encode(key).decode()) == key


def test_parse_key_rejects_short() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        parse_vault_master_key("nope")


def test_unset_uses_dev_key() -> None:
    assert vault_master_key_unset(None)
    assert vault_master_key_unset("")
    assert vault_master_key_unset("  ")
    assert not vault_master_key_unset("abc")
    assert vault_key_bytes(None) == DEV_VAULT_MASTER_KEY
