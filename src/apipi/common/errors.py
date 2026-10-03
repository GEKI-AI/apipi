"""Errors both the API and the worker raise."""

from typing import Any


class ApiError(Exception):
    def __init__(
        self,
        type: str,
        message: str,
        *,
        code: str = "",
        status_code: int = 400,
        session_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.type = type
        self.message = message
        self.code = code
        self.status_code = status_code
        self.session_id = session_id
        self.extra = extra or {}


class ObjectStoreError(Exception):
    def __init__(
        self,
        message: str,
        *,
        operation: str,
        bucket: str,
        key: str,
        code: str,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.bucket = bucket
        self.key = key
        self.code = code


def store_error(message: str, *, operation: str, key: str = "") -> ObjectStoreError:
    return ObjectStoreError(
        message, operation=operation, bucket="", key=key, code="artifact_store"
    )
