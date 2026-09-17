from uuid import UUID

from apipi.gateway.auth import AuthReject

calls: list[str] = []

TENANT = UUID("12345678-1234-5678-1234-567812345678")


def reset() -> None:
    calls.clear()


def accept(bearer: str) -> dict[str, str]:
    calls.append(bearer)
    return {"key_id": "plugin", "tenant_id": str(TENANT)}


def reject(bearer: str) -> None:
    calls.append(bearer)
    return None


def reject_typed(bearer: str) -> AuthReject:
    calls.append(bearer)
    return AuthReject(
        status_code=401,
        code="unauthorized",
        message="Expired key",
    )


def limit(bearer: str) -> AuthReject:
    calls.append(bearer)
    return AuthReject(
        status_code=429,
        code="rate_limited",
        message="Too many requests",
    )


def quota(bearer: str) -> dict[str, object]:
    calls.append(bearer)
    return {
        "status_code": 429,
        "code": "quota",
        "message": "No more agents for this tenant",
    }


def boom(bearer: str) -> None:
    calls.append(bearer)
    raise RuntimeError("plugin failed")
