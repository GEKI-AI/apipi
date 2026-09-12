from apipi.auth import AuthIdentity, authenticate as default_authenticate


def authenticate(bearer: str) -> AuthIdentity | None:
    if not bearer:
        return None
    return default_authenticate(bearer)
