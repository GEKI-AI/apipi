from apipi.gateway.auth import AuthIdentity, AuthReject
from apipi.gateway.auth import authenticate as default_authenticate


def authenticate(bearer: str) -> AuthIdentity | AuthReject:
    if bearer.startswith("limited-"):
        return AuthReject(
            status_code=429,
            code="rate_limited",
            message="Too many requests for this key",
        )
    if not bearer:
        return AuthReject(
            status_code=401,
            code="unauthorized",
            message="Invalid bearer token",
        )
    return default_authenticate(bearer)
