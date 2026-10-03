import base64
import hashlib
import hmac
import json
import os
import time

from apipi.gateway.auth import AuthIdentity

SECRET = os.environ.get("MODEL_CREDENTIAL_SECRET", "change-me").encode()
LIFETIME_SECONDS = 3600
PURPOSE = "model"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def model_credential(identity: AuthIdentity, bearer: str | None) -> str:
    claims = {
        "key_id": identity.key_id,
        "org_id": identity.org_id,
        "user_id": identity.user_id,
        "purpose": PURPOSE,
        "exp": int(time.time()) + LIFETIME_SECONDS,
    }
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signature = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def verify_model_credential(token: str) -> dict[str, object] | None:
    """The check the model host runs on `Authorization: Bearer <token>`."""
    body, _, signature = token.partition(".")
    expected = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    claims = json.loads(_unb64(body))
    if claims.get("purpose") != PURPOSE or claims.get("exp", 0) < time.time():
        return None
    return claims
