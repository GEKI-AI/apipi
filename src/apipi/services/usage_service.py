import uuid
from datetime import date

from apipi.gateway.auth import not_found
from apipi.gateway.errors import ApiError
from apipi.store.engine import Store
from apipi.store.repo import (
    get_session,
    get_turn,
    get_turn_log,
    usage_day,
    usage_totals,
)

_EMPTY_TURN = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "total_tokens": 0,
    "turns": 0,
}


class UsageService:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def get(
        self,
        tenant_id: uuid.UUID,
        *,
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
        async with self.store.session() as db:
            if session_id is not None:
                if await get_session(db, tenant_id, session_id) is None:
                    not_found()
                return await usage_totals(db, tenant_id, session_id=session_id)
            if turn_id is not None:
                turn = await get_turn(db, tenant_id, turn_id)
                if turn is None:
                    not_found()
                row = await get_turn_log(db, tenant_id, turn_id)
                if row is None:
                    return dict(_EMPTY_TURN)
                return {
                    "prompt_tokens": row.prompt_tokens,
                    "completion_tokens": row.completion_tokens,
                    "cache_read_tokens": row.cache_read_tokens,
                    "cache_write_tokens": row.cache_write_tokens,
                    "total_tokens": row.total_tokens,
                    "turns": 1,
                }
            if day is not None:
                return await usage_day(db, tenant_id, day)
        raise ApiError(
            "invalid_request",
            "Provide one of session_id, turn_id, or day",
            code="invalid_request",
        )
