import importlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Annotated, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.config import ConfigError
from apipi.errors import ApiError
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import ensure_tenant
from apipi.tokens import hash_token

_bearer = HTTPBearer(auto_error=False)
_MISS = object()
_REJECT = object()

Authenticate = Callable[[str], object]


@dataclass(frozen=True)
class AuthIdentity:
    key_id: str
    tenant_id: UUID


class AuthCache:
    def __init__(self, ttl: timedelta) -> None:
        self._ttl = ttl
        self._entries: dict[str, tuple[float, object]] = {}

    def get(self, key_hash: str) -> object:
        item = self._entries.get(key_hash)
        if item is None:
            return _MISS
        expires_at, value = item
        if monotonic() >= expires_at:
            del self._entries[key_hash]
            return _MISS
        return value

    def put(self, key_hash: str, value: object) -> None:
        self._entries[key_hash] = (monotonic() + self._ttl.total_seconds(), value)


def unauthorized() -> NoReturn:
    raise ApiError(
        "invalid_request",
        "Invalid bearer token",
        code="unauthorized",
        status_code=401,
    )


def not_found() -> NoReturn:
    raise ApiError("invalid_request", "Not found", code="not_found", status_code=404)


def authenticate(bearer: str) -> AuthIdentity:
    key_id = hash_token(bearer)
    return AuthIdentity(key_id=key_id, tenant_id=uuid5(NAMESPACE_URL, key_id))


def load_authenticate(path: str | None) -> Authenticate:
    if path is None or path == "":
        return authenticate
    if ":" not in path:
        raise ConfigError("APIPI_AUTH must be package.mod:func")
    module_name, func_name = path.rsplit(":", 1)
    if not module_name or not func_name:
        raise ConfigError("APIPI_AUTH must be package.mod:func")
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError("APIPI_AUTH must be package.mod:func") from exc
    if not callable(fn):
        raise ConfigError("APIPI_AUTH must be package.mod:func")
    return fn


def identity_from_result(result: object) -> AuthIdentity | None:
    if result is None:
        return None
    if isinstance(result, AuthIdentity):
        return result
    if isinstance(result, dict):
        key_id = result.get("key_id")
        tenant_id = result.get("tenant_id")
        if not key_id or tenant_id is None:
            raise TypeError("authenticate must return key_id and tenant_id")
        parsed = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
        return AuthIdentity(key_id=str(key_id), tenant_id=parsed)
    raise TypeError("authenticate must return key_id and tenant_id")


def _store(request: Request) -> Store:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        unauthorized()
    return store


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with _store(request).session() as session:
        yield session


async def require_tenant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    request: Request,
) -> Tenant:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        unauthorized()
    token = creds.credentials
    key_hash = hash_token(token)
    cache: AuthCache = request.app.state.auth_cache
    cached = cache.get(key_hash)
    if cached is _REJECT:
        unauthorized()
    if isinstance(cached, AuthIdentity):
        identity = cached
    else:
        fn: Authenticate = request.app.state.authenticate
        try:
            identity = identity_from_result(fn(token))
        except Exception as exc:
            raise ApiError(
                "invalid_request",
                "Invalid bearer token",
                code="unauthorized",
                status_code=401,
            ) from exc
        if identity is None:
            cache.put(key_hash, _REJECT)
            unauthorized()
        cache.put(key_hash, identity)
    request.state.tenant_id = identity.tenant_id
    request.state.key_id = identity.key_id
    async with _store(request).session() as db:
        return await ensure_tenant(db, identity.tenant_id)
