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
from apipi.gateway.errors import ApiError
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import ensure_tenant

_bearer = HTTPBearer(auto_error=False)
_MISS = object()

Authenticate = Callable[[str], object]


@dataclass(frozen=True)
class AuthIdentity:
    key_id: str
    tenant_id: UUID
    user_id: str | None = None
    thinking_summary: bool = False
    auto_title: bool = False


@dataclass(frozen=True)
class AuthReject:
    status_code: int
    code: str
    message: str
    type: str = "invalid_request"


UNAUTHORIZED = AuthReject(
    status_code=401,
    code="unauthorized",
    message="Invalid bearer token",
)


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


def raise_auth(reject: AuthReject) -> NoReturn:
    raise ApiError(
        reject.type,
        reject.message,
        code=reject.code,
        status_code=reject.status_code,
    )


def unauthorized() -> NoReturn:
    raise_auth(UNAUTHORIZED)


def not_found() -> NoReturn:
    raise ApiError("invalid_request", "Not found", code="not_found", status_code=404)


def tenant_from_key(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, hash_token(key))


def authenticate(bearer: str) -> AuthIdentity:
    key_id = hash_token(bearer)
    return AuthIdentity(key_id=key_id, tenant_id=tenant_from_key(bearer))


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


def _reject_from_dict(result: dict[str, object]) -> AuthReject:
    raw_status = result.get("status_code", 401)
    status_code = 401
    if isinstance(raw_status, int):
        status_code = raw_status
    elif isinstance(raw_status, str) and raw_status.isdigit():
        status_code = int(raw_status)
    code = result.get("code")
    message = result.get("message")
    err_type = result.get("type")
    if status_code == 429:
        default_code = "rate_limited"
        default_message = "Too many requests"
    else:
        default_code = "unauthorized"
        default_message = "Invalid bearer token"
    return AuthReject(
        status_code=status_code,
        code=str(code) if code else default_code,
        message=str(message) if message else default_message,
        type=str(err_type) if err_type else "invalid_request",
    )


def auth_from_result(result: object) -> AuthIdentity | AuthReject:
    if result is None:
        return UNAUTHORIZED
    if isinstance(result, AuthReject):
        return result
    if isinstance(result, AuthIdentity):
        return result
    if isinstance(result, dict):
        key_id = result.get("key_id")
        tenant_id = result.get("tenant_id")
        if key_id and tenant_id is not None:
            parsed = tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id))
            raw_user = result.get("user_id")
            user_id = str(raw_user) if raw_user else None
            return AuthIdentity(
                key_id=str(key_id),
                tenant_id=parsed,
                user_id=user_id,
                thinking_summary=result.get("thinking_summary") is True,
                auto_title=result.get("auto_title") is True,
            )
        if "status_code" in result or "code" in result:
            return _reject_from_dict(result)
        raise TypeError("authenticate must return key_id and tenant_id")
    raise TypeError("authenticate must return key_id and tenant_id")


def identity_from_result(result: object) -> AuthIdentity | None:
    parsed = auth_from_result(result)
    if isinstance(parsed, AuthIdentity):
        return parsed
    return None


def _store(request: Request) -> Store:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        unauthorized()
    return store


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with _store(request).session() as session:
        yield session


def _cache_reject(reject: AuthReject) -> bool:
    return reject.status_code != 429


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
    if isinstance(cached, AuthReject):
        raise_auth(cached)
    if isinstance(cached, AuthIdentity):
        identity = cached
    else:
        fn: Authenticate = request.app.state.authenticate
        try:
            parsed = auth_from_result(fn(token))
        except Exception as exc:
            raise ApiError(
                "invalid_request",
                "Invalid bearer token",
                code="unauthorized",
                status_code=401,
            ) from exc
        if isinstance(parsed, AuthReject):
            if _cache_reject(parsed):
                cache.put(key_hash, parsed)
            raise_auth(parsed)
        cache.put(key_hash, parsed)
        identity = parsed
    request.state.tenant_id = identity.tenant_id
    request.state.key_id = identity.key_id
    request.state.user_id = identity.user_id
    request.state.thinking_summary = identity.thinking_summary
    request.state.auto_title = identity.auto_title
    request.state.bearer = token
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None:
        return await gateway.ensure_tenant(identity.tenant_id)
    async with _store(request).session() as db:
        return await ensure_tenant(db, identity.tenant_id)
