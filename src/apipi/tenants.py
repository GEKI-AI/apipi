from sqlalchemy.ext.asyncio import AsyncSession

from apipi.store.models import Tenant
from apipi.store.repo import create_api_key, create_tenant
from apipi.tokens import generate_token, hash_token


async def provision_tenant(db: AsyncSession, *, name: str) -> tuple[Tenant, str]:
    tenant = await create_tenant(db, name=name)
    token = generate_token()
    await create_api_key(db, tenant.id, token_hash=hash_token(token))
    return tenant, token
