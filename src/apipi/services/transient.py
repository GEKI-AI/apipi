"""Tell a temporary failure from a permanent one.

Ingest never acks past a temporary failure: the envelope is tried
again, or the socket closes and the worker replays it. A permanent
failure is a verdict on the envelope itself, so it is rejected and
acked past.
"""

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from apipi.common.errors import ObjectStoreError

_SQLSTATES = frozenset(
    {
        "40001",
        "40P01",
        "55P03",
        "57014",
        "57P01",
        "57P03",
        "53300",
        "08000",
        "08001",
        "08003",
        "08006",
    }
)
_STORE_CODES = frozenset(
    {
        "SlowDown",
        "RequestTimeout",
        "RequestTimeoutException",
        "InternalError",
        "ServiceUnavailable",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "ConnectionError",
        "ProxyConnectionError",
        "ResponseStreamingError",
        "500",
        "502",
        "503",
        "504",
    }
)


def is_transient(exc: BaseException) -> bool:
    """True when the same work may succeed if it is tried again."""
    if isinstance(exc, ObjectStoreError):
        if exc.code in _STORE_CODES:
            return True
        cause = exc.__cause__
        return cause is not None and is_transient(cause)
    if isinstance(exc, DBAPIError):
        if exc.connection_invalidated:
            return True
        state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if isinstance(state, str) and state in _SQLSTATES:
            return True
        return isinstance(exc, OperationalError | InterfaceError)
    return isinstance(exc, TimeoutError | ConnectionError)
