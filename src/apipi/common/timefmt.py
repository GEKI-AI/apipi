"""Timestamp formatting shared by the API and the worker."""

from datetime import UTC, datetime


def utc_ts(moment: datetime | None = None) -> str:
    now = moment or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    text = now.astimezone(UTC).isoformat(timespec="milliseconds")
    if text.endswith("+00:00"):
        return text[:-6] + "Z"
    return text
