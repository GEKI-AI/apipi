"""Per-worker bearer tokens for `/internal/worker`.

Only the SHA-256 hash of a token is stored. The secret is shown once
at creation and never again. A token is bound to one `worker_id`,
either declared at creation or on first register. Several active
tokens per worker allow rotation. Revocation closes live sockets.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from apipi.store.engine import Store
from apipi.store.models import WorkerToken
from apipi.store.repo import (
    create_worker_token,
    find_worker_token,
    get_worker_token,
    list_worker_tokens,
    revoke_worker_token,
    touch_worker_token,
)


@dataclass(frozen=True)
class CreatedWorkerToken:
    row: WorkerToken
    secret: str


def generate_worker_secret() -> str:
    return secrets.token_urlsafe(32)


def hash_worker_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


async def create_token(
    store: Store,
    *,
    name: str,
    worker_id: uuid.UUID | None = None,
) -> CreatedWorkerToken:
    secret = generate_worker_secret()
    async with store.session() as db:
        row = await create_worker_token(
            db,
            name=name,
            token_hash=hash_worker_secret(secret),
            worker_id=worker_id,
        )
    return CreatedWorkerToken(row=row, secret=secret)


async def list_tokens(store: Store) -> list[WorkerToken]:
    async with store.session() as db:
        return await list_worker_tokens(db)


async def revoke_token(store: Store, token_id: uuid.UUID) -> WorkerToken | None:
    async with store.session() as db:
        return await revoke_worker_token(db, token_id)


async def authenticate_token(store: Store, secret: str) -> WorkerToken | None:
    """Return the live token row for a presented secret, or None."""
    async with store.session() as db:
        row = await find_worker_token(db, hash_worker_secret(secret))
        if row is None or row.revoked_at is not None:
            return None
        await touch_worker_token(db, row)
        return row


async def is_revoked_secret(store: Store, secret: str) -> bool:
    async with store.session() as db:
        row = await find_worker_token(db, hash_worker_secret(secret))
        return row is not None and row.revoked_at is not None


async def token_revoked(store: Store, token_id: uuid.UUID) -> bool:
    async with store.session() as db:
        row = await get_worker_token(db, token_id)
        return row is None or row.revoked_at is not None
