import uuid
from datetime import timedelta

from apipi.gateway.auth import AuthCache, AuthIdentity
from apipi.gateway.tokens import hash_token


def _identity(key: str) -> AuthIdentity:
    return AuthIdentity(key_id=key, tenant_id=uuid.uuid4(), user_id=None, org_id=None)


def test_lru_evicts_oldest() -> None:
    cache = AuthCache(timedelta(seconds=30), max_entries=2)
    cache.put(hash_token("a"), _identity("a"))
    cache.put(hash_token("b"), _identity("b"))
    assert len(cache) == 2
    cache.put(hash_token("c"), _identity("c"))
    assert len(cache) == 2
    assert cache.get(hash_token("a")) is not _identity("a")
    from apipi.gateway.auth import _MISS

    assert cache.get(hash_token("a")) is _MISS
    assert cache.get(hash_token("b")) is not _MISS
    assert cache.get(hash_token("c")) is not _MISS


def test_lru_refreshes_recency() -> None:
    from apipi.gateway.auth import _MISS

    cache = AuthCache(timedelta(seconds=30), max_entries=2)
    cache.put(hash_token("a"), _identity("a"))
    cache.put(hash_token("b"), _identity("b"))
    assert cache.get(hash_token("a")) is not _MISS
    cache.put(hash_token("c"), _identity("c"))
    assert cache.get(hash_token("b")) is _MISS
    assert cache.get(hash_token("a")) is not _MISS


def test_zero_disables_caching() -> None:
    from apipi.gateway.auth import _MISS

    cache = AuthCache(timedelta(seconds=30), max_entries=0)
    cache.put(hash_token("a"), _identity("a"))
    assert len(cache) == 0
    assert cache.get(hash_token("a")) is _MISS


def test_invalidate_and_clear() -> None:
    cache = AuthCache(timedelta(seconds=30), max_entries=10)
    cache.put(hash_token("a"), _identity("a"))
    cache.put(hash_token("b"), _identity("b"))
    assert cache.invalidate(hash_token("a")) is True
    assert cache.invalidate(hash_token("a")) is False
    assert len(cache) == 1
    assert cache.clear() == 1
    assert len(cache) == 0


def test_invalidate_where_matches_identities_only() -> None:
    from apipi.gateway.auth import AuthReject

    cache = AuthCache(timedelta(seconds=30), max_entries=10)
    tid = uuid.uuid4()
    cache.put(
        hash_token("a"),
        AuthIdentity(key_id="a", tenant_id=tid, user_id="u1"),
    )
    cache.put(
        hash_token("b"),
        AuthIdentity(key_id="b", tenant_id=tid, user_id="u2"),
    )
    cache.put(
        hash_token("c"),
        AuthReject(status_code=401, code="unauthorized", message="bad"),
    )
    assert cache.invalidate_where(lambda i: i.user_id == "u1") == 1
    assert len(cache) == 2
