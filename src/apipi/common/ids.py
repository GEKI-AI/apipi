"""Small id helpers."""

import uuid


def uuid_or_none(raw: str | None) -> uuid.UUID | None:
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None
