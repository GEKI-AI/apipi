from uuid import NAMESPACE_URL, uuid5

import pytest

from apipi.auth import authenticate, load_authenticate
from apipi.config import ConfigError
from apipi.tokens import hash_token


def test_hash_token_is_sha256_hex() -> None:
    digest = hash_token("secret")
    assert len(digest) == 64
    assert digest == hash_token("secret")
    assert digest != hash_token("other")


def test_authenticate_hashes_and_uuid5() -> None:
    identity = authenticate("secret")
    assert identity.key_id == hash_token("secret")
    assert identity.tenant_id == uuid5(NAMESPACE_URL, identity.key_id)


def test_same_bearer_same_tenant() -> None:
    assert authenticate("same").tenant_id == authenticate("same").tenant_id


def test_different_bearers_different_tenants() -> None:
    assert authenticate("a").tenant_id != authenticate("b").tenant_id


def test_load_authenticate_default() -> None:
    assert load_authenticate(None) is authenticate
    assert load_authenticate("") is authenticate


def test_load_authenticate_bad_path() -> None:
    with pytest.raises(ConfigError, match="APIPI_AUTH"):
        load_authenticate("not-a-path")
