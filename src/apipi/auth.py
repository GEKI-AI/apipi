from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, NoReturn
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.errors import ApiError
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import ensure_tenant
from apipi.tokens import hash_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class AuthIdentity:
    key_id: str
    tenant_id: UUID


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
    identity = authenticate(creds.credentials)
    async with _store(request).session() as db:
        return await ensure_tenant(db, identity.tenant_id)
