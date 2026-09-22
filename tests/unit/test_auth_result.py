from uuid import UUID

import pytest

from apipi.gateway.auth import (
    UNAUTHORIZED,
    AuthIdentity,
    AuthReject,
    auth_from_result,
    authenticate,
    tenant_from_key,
)
from apipi.gateway.tokens import hash_token


def test_tenant_from_key_matches_default_authenticate() -> None:
    identity = authenticate("secret")
    assert identity.key_id == hash_token("secret")
    assert identity.tenant_id == tenant_from_key("secret")
    assert tenant_from_key("secret") != tenant_from_key("other")


def test_none_is_unauthorized() -> None:
    assert auth_from_result(None) == UNAUTHORIZED


def test_identity_passthrough() -> None:
    identity = AuthIdentity(
        key_id="k", tenant_id=UUID("12345678-1234-5678-1234-567812345678")
    )
    assert auth_from_result(identity) is identity


def test_reject_passthrough() -> None:
    reject = AuthReject(status_code=429, code="rate_limited", message="slow down")
    assert auth_from_result(reject) is reject


def test_identity_from_dict() -> None:
    parsed = auth_from_result(
        {"key_id": "k", "tenant_id": "12345678-1234-5678-1234-567812345678"}
    )
    assert isinstance(parsed, AuthIdentity)
    assert parsed.key_id == "k"
    assert parsed.user_id is None


def test_thinking_summary_defaults_off() -> None:
    parsed = auth_from_result(
        {"key_id": "k", "tenant_id": "12345678-1234-5678-1234-567812345678"}
    )
    assert isinstance(parsed, AuthIdentity)
    assert parsed.thinking_summary is False


def test_thinking_summary_accepts_only_true() -> None:
    tenant = "12345678-1234-5678-1234-567812345678"
    on = auth_from_result(
        {"key_id": "k", "tenant_id": tenant, "thinking_summary": True}
    )
    off = auth_from_result(
        {"key_id": "k", "tenant_id": tenant, "thinking_summary": "true"}
    )
    assert isinstance(on, AuthIdentity)
    assert on.thinking_summary is True
    assert isinstance(off, AuthIdentity)
    assert off.thinking_summary is False


def test_auto_title_accepts_only_true() -> None:
    tenant = "12345678-1234-5678-1234-567812345678"
    on = auth_from_result({"key_id": "k", "tenant_id": tenant, "auto_title": True})
    off = auth_from_result({"key_id": "k", "tenant_id": tenant, "auto_title": 1})
    assert isinstance(on, AuthIdentity)
    assert on.auto_title is True
    assert isinstance(off, AuthIdentity)
    assert off.auto_title is False


def test_identity_from_dict_with_user_id() -> None:
    parsed = auth_from_result(
        {
            "key_id": "k",
            "tenant_id": "12345678-1234-5678-1234-567812345678",
            "user_id": "user-9",
        }
    )
    assert isinstance(parsed, AuthIdentity)
    assert parsed.user_id == "user-9"


def test_reject_from_dict() -> None:
    parsed = auth_from_result(
        {"status_code": 429, "code": "quota", "message": "plan cap"}
    )
    assert parsed == AuthReject(status_code=429, code="quota", message="plan cap")


def test_unknown_result_raises() -> None:
    with pytest.raises(TypeError, match="key_id and tenant_id"):
        auth_from_result("nope")
