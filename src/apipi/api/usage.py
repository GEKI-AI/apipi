import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apipi.auth import get_db, not_found, require_tenant
from apipi.errors import ApiError
from apipi.store.models import Tenant
from apipi.store.repo import get_session, get_turn_log, usage_totals

router = APIRouter()


@router.get("/v1/usage")
async def get_usage(
    tenant: Annotated[Tenant, Depends(require_tenant)],
    db: Annotated[AsyncSession, Depends(get_db)],
    session_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
    day: date | None = None,
) -> dict[str, int]:
    if sum(value is not None for value in (session_id, turn_id, day)) != 1:
        raise ApiError(
            "invalid_request",
            "Provide one of session_id, turn_id, or day",
            code="invalid_request",
        )
    if session_id is not None:
        if await get_session(db, tenant.id, session_id) is None:
            not_found()
        return await usage_totals(db, tenant.id, session_id=session_id)
    if turn_id is not None:
        row = await get_turn_log(db, tenant.id, turn_id)
        if row is None:
            not_found()
        return {
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_tokens": row.cache_write_tokens,
            "total_tokens": row.total_tokens,
            "turns": 1,
        }
    if day is not None:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return await usage_totals(
            db, tenant.id, since=start, until=start + timedelta(days=1)
        )
    raise ApiError(
        "invalid_request",
        "Provide one of session_id, turn_id, or day",
        code="invalid_request",
    )
