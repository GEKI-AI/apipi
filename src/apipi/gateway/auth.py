import asyncio
import contextlib
import importlib
import inspect
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from types import MappingProxyType
from typing import Annotated, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apipi.config import ConfigError
from apipi.gateway.errors import ApiError
from apipi.gateway.tokens import hash_token
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import ensure_tenant

_bearer = HTTPBearer(auto_error=False)
_MISS = object()

Authenticate = Callable[..., object]
Authorize = Callable[..., object]


@dataclass(frozen=True)
class AuthRequest:
    method: str
    path: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class AuthIdentity:
    key_id: str
    tenant_id: UUID
    user_id: str | None = None
    org_id: str | None = None
    cache_key: str | None = None


@dataclass(frozen=True)
class AuthReject:
    status_code: int
    code: str
    message: str
    type: str = "invalid_request"
    cache_key: str | None = None


UNAUTHORIZED = AuthReject(
    status_code=401,
    code="unauthorized",
    message="Invalid bearer token",
)


@dataclass(frozen=True)
class AuthFilter:
    ids: frozenset[str] | None = None


@dataclass(frozen=True)
class AuthContext:
    identity: AuthIdentity
    request: AuthRequest


class AuthCache:
    def __init__(self, ttl: timedelta, max_entries: int = 10000) -> None:
        self._ttl = ttl
        self._max = max(0, max_entries)
        self._entries: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self.evictions = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def max_entries(self) -> int:
        return self._max

    def get(self, key_hash: str) -> object:
        if self._max == 0:
            return _MISS
        item = self._entries.get(key_hash)
        if item is None:
            return _MISS
        expires_at, value = item
        if monotonic() >= expires_at:
            del self._entries[key_hash]
            return _MISS
        self._entries.move_to_end(key_hash)
        return value

    def put(self, key_hash: str, value: object) -> None:
        if self._max == 0:
            return
        if key_hash in self._entries:
            del self._entries[key_hash]
        self._entries[key_hash] = (monotonic() + self._ttl.total_seconds(), value)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
            self.evictions += 1

    def invalidate(self, key_hash: str) -> bool:
        return self._entries.pop(key_hash, None) is not None

    def invalidate_where(self, predicate: Callable[[AuthIdentity], bool]) -> int:
        doomed = [
            key
            for key, (_, value) in self._entries.items()
            if isinstance(value, AuthIdentity)
            and self._fresh(value, key)
            and _safe_predicate(predicate, value)
        ]
        for key in doomed:
            self._entries.pop(key, None)
        return len(doomed)

    def _fresh(self, _value: object, key: str) -> bool:
        item = self._entries.get(key)
        if item is None:
            return False
        expires_at, _ = item
        if monotonic() >= expires_at:
            del self._entries[key]
            return False
        return True

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        return count


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


def _safe_predicate(
    predicate: Callable[[AuthIdentity], bool], value: AuthIdentity
) -> bool:
    try:
        return bool(predicate(value))
    except Exception:
        return False


def load_authorize(path: str | None) -> Authorize | None:
    if path is None or path == "":
        return None
    if ":" not in path:
        raise ConfigError("APIPI_AUTHORIZE must be package.mod:func")
    module_name, func_name = path.rsplit(":", 1)
    if not module_name or not func_name:
        raise ConfigError("APIPI_AUTHORIZE must be package.mod:func")
    try:
        module = importlib.import_module(module_name)
        fn = getattr(module, func_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError("APIPI_AUTHORIZE must be package.mod:func") from exc
    if not callable(fn):
        raise ConfigError("APIPI_AUTHORIZE must be package.mod:func")
    return fn


async def _call_off_loop(fn: Callable[..., object], *args: object) -> object:
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    result = await asyncio.to_thread(fn, *args)
    if inspect.isawaitable(result):
        return await result
    return result


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
    sibling = getattr(module, "cache_key", None)
    if (
        callable(sibling)
        and sibling is not fn
        and getattr(fn, "cache_key", None) is None
    ):
        with contextlib.suppress(AttributeError, TypeError):
            fn.cache_key = sibling
    return fn


def auth_request_of(request: Request) -> AuthRequest:
    headers = {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() != "authorization"
    }
    return AuthRequest(
        method=request.method,
        path=request.url.path,
        headers=MappingProxyType(headers),
    )


def _takes_context(fn: Callable[..., object]) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in params):
        return True
    positional = [
        param
        for param in params
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 2


async def _invoke_off_loop(
    fn: Callable[..., object], token: str, ctx: AuthRequest
) -> object:
    if _takes_context(fn):
        return await _call_off_loop(fn, token, ctx)
    return await _call_off_loop(fn, token)


def invoke_authenticate(
    fn: Callable[..., object], token: str, ctx: AuthRequest
) -> object:
    if _takes_context(fn):
        return fn(token, ctx)
    return fn(token)


async def _plugin_cache_key(
    fn: Callable[..., object], token: str, ctx: AuthRequest
) -> str | None:
    cache_fn = getattr(fn, "cache_key", None)
    if not callable(cache_fn):
        return None
    try:
        if _takes_context(cache_fn):
            raw = await _call_off_loop(cache_fn, token, ctx)
        else:
            raw = await _call_off_loop(cache_fn, token)
    except Exception:
        return None
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


async def resolve_cache_key(
    fn: Callable[..., object], token: str, ctx: AuthRequest
) -> str:
    explicit = await _plugin_cache_key(fn, token, ctx)
    if explicit is not None:
        return hash_token(explicit)
    return hash_token(token)


def _text_cache_key(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


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
        cache_key=_text_cache_key(result.get("cache_key")),
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
            raw_org = result.get("org_id")
            org_id = str(raw_org) if raw_org else None
            return AuthIdentity(
                key_id=str(key_id),
                tenant_id=parsed,
                user_id=user_id,
                org_id=org_id,
                cache_key=_text_cache_key(result.get("cache_key")),
            )
        if "status_code" in result or "code" in result:
            return _reject_from_dict(result)
        raise TypeError("authenticate must return key_id and tenant_id")
    raise TypeError("authenticate must return key_id and tenant_id")


def _store(request: Request) -> Store:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        unauthorized()
    return store


def _cache_reject(reject: AuthReject) -> bool:
    return reject.status_code != 429


def stored_cache_key(parsed: AuthIdentity | AuthReject, fallback: str) -> str:
    if isinstance(parsed.cache_key, str) and parsed.cache_key.strip():
        return hash_token(parsed.cache_key)
    return fallback


_flights: dict[str, asyncio.Task[object]] = {}
_flight_lock = asyncio.Lock()


async def _authenticate_single_flight(
    fn: Authenticate, token: str, ctx: AuthRequest, cache: AuthCache
) -> AuthIdentity | AuthReject:
    key_hash = await resolve_cache_key(fn, token, ctx)
    cached = cache.get(key_hash)
    if isinstance(cached, (AuthIdentity, AuthReject)):
        return cached
    async with _flight_lock:
        existing = _flights.get(key_hash)
        if existing is not None:
            task = existing
        else:
            task = asyncio.ensure_future(_run_auth(fn, token, ctx, cache, key_hash))
            _flights[key_hash] = task
    try:
        result = await task
        assert isinstance(result, (AuthIdentity, AuthReject))
        return result
    finally:
        async with _flight_lock:
            if _flights.get(key_hash) is task:
                _flights.pop(key_hash, None)


async def _run_auth(
    fn: Authenticate, token: str, ctx: AuthRequest, cache: AuthCache, key_hash: str
) -> AuthIdentity | AuthReject:
    try:
        parsed = auth_from_result(await _invoke_off_loop(fn, token, ctx))
    except Exception as exc:
        raise ApiError(
            "invalid_request",
            "Invalid bearer token",
            code="unauthorized",
            status_code=401,
        ) from exc
    store_key = stored_cache_key(parsed, key_hash)
    if isinstance(parsed, AuthReject):
        if _cache_reject(parsed):
            cache.put(store_key, parsed)
        return parsed
    cache.put(store_key, parsed)
    return parsed


def identity_of(request: Request) -> AuthIdentity | None:
    tenant_id = getattr(request.state, "tenant_id", None)
    key_id = getattr(request.state, "key_id", None)
    if tenant_id is None or key_id is None:
        return None
    user_id = getattr(request.state, "user_id", None)
    org_id = getattr(request.state, "org_id", None)
    return AuthIdentity(
        key_id=str(key_id),
        tenant_id=tenant_id if isinstance(tenant_id, UUID) else UUID(str(tenant_id)),
        user_id=str(user_id) if user_id else None,
        org_id=str(org_id) if org_id else None,
    )


async def check_authorize(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
) -> AuthFilter | None:
    authorize = getattr(request.app.state, "authorize", None)
    if authorize is None:
        return None
    identity = identity_of(request)
    if identity is None:
        return None
    ctx = auth_request_of(request)
    try:
        if _takes_authorize_context(authorize):
            raw = await _call_off_loop(
                authorize, identity, action, resource_type, resource_id, ctx
            )
        else:
            raw = await _call_off_loop(
                authorize, identity, action, resource_type, resource_id
            )
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            "invalid_request", "Forbidden", code="forbidden", status_code=403
        ) from exc
    if raw is None:
        return None
    if isinstance(raw, AuthReject):
        raise_auth(raw)
    if isinstance(raw, AuthFilter):
        if not action.endswith(".list"):
            raise ApiError(
                "invalid_request", "Forbidden", code="forbidden", status_code=403
            )
        return raw
    if isinstance(raw, dict) and "ids" in raw:
        ids = raw.get("ids")
        parsed = frozenset(str(i) for i in ids) if isinstance(ids, list) else None
        return AuthFilter(ids=parsed)
    return None


def _takes_authorize_context(fn: Callable[..., object]) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        p
        for p in params
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    return len(positional) >= 5


def forbidden() -> NoReturn:
    raise ApiError("invalid_request", "Forbidden", code="forbidden", status_code=403)


async def require_tenant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    request: Request,
) -> Tenant:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        unauthorized()
    token = creds.credentials
    ctx = auth_request_of(request)
    fn: Authenticate = request.app.state.authenticate
    cache: AuthCache = request.app.state.auth_cache
    parsed = await _authenticate_single_flight(fn, token, ctx, cache)
    if isinstance(parsed, AuthReject):
        raise_auth(parsed)
    identity = parsed
    request.state.tenant_id = identity.tenant_id
    request.state.key_id = identity.key_id
    request.state.user_id = identity.user_id
    request.state.org_id = identity.org_id
    request.state.bearer = token
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is not None:
        return await gateway.ensure_tenant(identity.tenant_id)
    async with _store(request).session() as db:
        return await ensure_tenant(db, identity.tenant_id)
