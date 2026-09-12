from collections.abc import AsyncIterator
from typing import Annotated, NoReturn

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.errors import ApiError
from apipi.store.engine import Store
from apipi.store.models import Tenant
from apipi.store.repo import get_api_key_by_hash, get_tenant
from apipi.tokens import hash_token, token_matches

_bearer = HTTPBearer(auto_error=False)


def unauthorized() -> NoReturn:
    raise ApiError(
        "invalid_request",
        "Invalid bearer token",
        code="unauthorized",
        status_code=401,
    )


def not_found() -> NoReturn:
    raise ApiError("invalid_request", "Not found", code="not_found", status_code=404)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    store: Store | None = getattr(request.app.state, "store", None)
    if store is None:
        unauthorized()
    async with store.session() as session:
        yield session


async def require_tenant(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Tenant:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        unauthorized()
    token = creds.credentials
    key = await get_api_key_by_hash(db, hash_token(token))
    if key is None or not token_matches(token, key.token_hash):
        unauthorized()
    tenant = await get_tenant(db, key.tenant_id)
    if tenant is None:
        unauthorized()
    return tenant
