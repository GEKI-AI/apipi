import contextlib
import ipaddress
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NoReturn
from urllib.parse import urlparse

from apipi.common.errors import ApiError
from apipi.common.guest_env import ENV_NAME, reserved_secret_name
from apipi.config import Settings
from apipi.env.setup import SetupError, package_egress_hosts, session_network_from
from apipi.services.vault_crypto import (
    VaultCryptoError,
    decrypt_vault_token,
    vault_aad,
    vault_key_bytes,
)
from apipi.store.models import VaultCredential
from apipi.store.repo import list_credentials_for_vault_ids

STATIC_BEARER = "static_bearer"
ENVIRONMENT_VARIABLE = "environment_variable"
GIT_USERNAME_KEY = "apipi.git_username"
MAX_ALLOWED_HOSTS = 100
MAX_SECRET_NAME = 255
MIN_SECRET_VALUE = 8
MAX_SECRET_VALUE = 16_384
MAX_GIT_USERNAME = 255
MAX_METADATA_KEYS = 16
MAX_METADATA_KEY = 64
MAX_METADATA_VALUE = 512

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SECRET_VALUE = re.compile(r"[\x21-\x7e]+")
_AUTH_KEYS = frozenset({"type", "secret_name", "secret_value", "networking"})
_NETWORKING_KEYS = frozenset({"type", "allowed_hosts"})


@dataclass(frozen=True)
class EnvironmentAuth:
    secret_name: str
    secret_value: str
    allowed_hosts: tuple[str, ...]


def _bad(message: str, code: str = "invalid_request") -> NoReturn:
    raise ApiError("invalid_request", message, code=code)


def _unknown(keys: Iterable[object], allowed: frozenset[str], prefix: str) -> None:
    for key in keys:
        if key not in allowed:
            _bad(f"Unknown field: {prefix}{key}", code="unknown_field")


def parse_secret_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        _bad("secret_name must be a non-empty string")
    if len(value) > MAX_SECRET_NAME or ENV_NAME.fullmatch(value) is None:
        _bad(
            "secret_name must be an environment variable name "
            "(letters, digits, and underscore, not starting with a digit)"
        )
    if reserved_secret_name(value):
        _bad(f"secret_name {value} is reserved")
    return value


def parse_secret_value(value: object) -> str:
    if not isinstance(value, str) or not value:
        _bad("secret_value must be a non-empty string")
    if len(value) < MIN_SECRET_VALUE:
        _bad(f"secret_value is at least {MIN_SECRET_VALUE} characters")
    if len(value) > MAX_SECRET_VALUE:
        _bad(f"secret_value is at most {MAX_SECRET_VALUE} characters")
    if _SECRET_VALUE.fullmatch(value) is None:
        _bad(
            "secret_value must be printable ASCII without spaces, "
            "line breaks, or other control characters"
        )
    return value


def parse_allowed_host(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _bad("allowed_hosts entries must be hostnames")
    host = value.strip().lower()
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        _bad(f"allowed_hosts entry {value} is an IP address; use a hostname")
    labels = host.split(".")
    if (
        len(host) > 253
        or not all(_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        _bad(
            f"allowed_hosts entry {value} must be an exact hostname "
            "without scheme, port, path, or wildcard"
        )
    return host


def parse_networking(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        _bad('networking must be {"type": "limited", "allowed_hosts": [...]}')
    _unknown(value, _NETWORKING_KEYS, "auth.networking.")
    if value.get("type") != "limited":
        _bad("networking.type must be limited")
    raw = value.get("allowed_hosts")
    if not isinstance(raw, list) or not raw:
        _bad("networking.allowed_hosts needs at least one hostname")
    if len(raw) > MAX_ALLOWED_HOSTS:
        _bad(f"networking.allowed_hosts is at most {MAX_ALLOWED_HOSTS} hostnames")
    hosts: list[str] = []
    for item in raw:
        host = parse_allowed_host(item)
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


def parse_environment_auth(auth: dict[str, Any]) -> EnvironmentAuth:
    _unknown(auth, _AUTH_KEYS, "auth.")
    return EnvironmentAuth(
        secret_name=parse_secret_name(auth.get("secret_name")),
        secret_value=parse_secret_value(auth.get("secret_value")),
        allowed_hosts=parse_networking(auth.get("networking")),
    )


def parse_environment_update(auth: dict[str, Any], row: VaultCredential) -> str | None:
    _unknown(auth, _AUTH_KEYS, "auth.")
    if "secret_name" in auth and auth["secret_name"] != row.secret_name:
        _bad("secret_name cannot change; create a new credential")
    if "networking" in auth and set(parse_networking(auth["networking"])) != set(
        row.allowed_hosts or []
    ):
        _bad("networking cannot change; create a new credential")
    if "secret_value" not in auth:
        return None
    return parse_secret_value(auth["secret_value"])


def credential_metadata(
    metadata: dict[str, Any] | None, auth_type: str
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    if len(metadata) > MAX_METADATA_KEYS:
        _bad(f"metadata has at most {MAX_METADATA_KEYS} keys")
    for key, value in metadata.items():
        if not key or len(key) > MAX_METADATA_KEY:
            _bad(f"metadata keys are 1 to {MAX_METADATA_KEY} characters")
        if not isinstance(value, str) or len(value) > MAX_METADATA_VALUE:
            _bad(
                "metadata values are strings of at most "
                f"{MAX_METADATA_VALUE} characters"
            )
    if GIT_USERNAME_KEY not in metadata:
        return metadata
    if auth_type != ENVIRONMENT_VARIABLE:
        _bad(f"{GIT_USERNAME_KEY} is only valid on environment_variable credentials")
    value = metadata[GIT_USERNAME_KEY]
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_GIT_USERNAME
        or _CONTROL.search(value)
        or ":" in value
        or value != value.strip()
    ):
        _bad(
            f"{GIT_USERNAME_KEY} must be a non-empty user name without "
            "colons, line breaks, or surrounding spaces"
        )
    return metadata


def networking_body(hosts: Iterable[str] | None) -> dict[str, Any]:
    return {"type": "limited", "allowed_hosts": list(hosts or [])}


def env_credential_rows(rows: Iterable[VaultCredential]) -> list[VaultCredential]:
    found = [row for row in rows if row.auth_type == ENVIRONMENT_VARIABLE]
    return sorted(found, key=lambda row: (row.secret_name or "", str(row.id)))


def _url_host(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).hostname
    return host.lower() if host else None


def operator_hosts(settings: Settings, environment: dict[str, Any]) -> set[str]:
    hosts: set[str] = set()
    if settings.model_base_url:
        host = _url_host(settings.model_base_url)
        if host:
            hosts.add(host)
    for item in settings.microvm_egress_hosts.split(","):
        host = _url_host(item)
        if host:
            hosts.add(host)
    with contextlib.suppress(SetupError):
        hosts.update(host.lower() for host in package_egress_hosts(environment))
    return hosts


def check_env_credentials(
    settings: Settings, environment: dict[str, Any], rows: list[VaultCredential]
) -> None:
    creds = env_credential_rows(rows)
    if not creds:
        return
    first = creds[0].secret_name
    if environment.get("type") == "none":
        _bad(
            f"environment_variable credential {first} needs a microVM session, "
            "and environment.type none has no computer. Use environment.type "
            "openai_hosted or attach a vault without environment credentials",
            code="credential_not_allowed",
        )
    try:
        policy = session_network_from(environment)
    except SetupError:
        policy = None
    if policy is not None and policy.access == "disabled":
        _bad(
            f"environment_variable credential {first} needs network access, "
            "and environment.network.access is disabled",
            code="credential_not_allowed",
        )
    seen: set[str] = set()
    for row in creds:
        name = row.secret_name or ""
        if name in seen:
            _bad(
                f"secret_name {name} is set by more than one attached vault credential",
                code="secret_name_collision",
            )
        seen.add(name)
    env_values = environment.get("env")
    if isinstance(env_values, dict):
        for name in sorted(seen & set(env_values)):
            _bad(
                f"secret_name {name} is also set in environment.env",
                code="secret_name_collision",
            )
    if not settings.microvm_egress_allowlist:
        return
    allowed = operator_hosts(settings, environment)
    for row in creds:
        for host in row.allowed_hosts or []:
            if host.lower() not in allowed:
                _bad(
                    f"credential host {host} of {row.secret_name} is not "
                    "allowed by the operator egress allowlist",
                    code="credential_host_not_allowed",
                )


def _decrypt(settings: Settings, row: VaultCredential) -> str:
    try:
        return decrypt_vault_token(
            row.token,
            vault_key_bytes(settings.vault_master_key),
            aad=vault_aad(row.tenant_id, row.id),
        )
    except VaultCryptoError as exc:
        raise ApiError(
            "api_error",
            "vault credential decrypt failed",
            code="internal",
            status_code=500,
        ) from exc


def resolve_env_credentials(
    settings: Settings, rows: list[VaultCredential]
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for row in env_credential_rows(rows):
        metadata = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        username = metadata.get(GIT_USERNAME_KEY)
        resolved.append(
            {
                "credential_id": str(row.id),
                "secret_name": row.secret_name or "",
                "secret_value": _decrypt(settings, row),
                "allowed_hosts": list(row.allowed_hosts or []),
                "git_username": username if isinstance(username, str) else None,
            }
        )
    return resolved


def session_vault_ids(raw: object) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            ids.append(uuid.UUID(str(item)))
        except ValueError:
            continue
    return ids


async def session_vault_secrets(
    db: Any, settings: Settings, tenant_id: uuid.UUID, vault_ids: list[uuid.UUID]
) -> tuple[str, ...]:
    rows = await list_credentials_for_vault_ids(db, tenant_id, vault_ids)
    secrets: list[str] = []
    for row in rows:
        try:
            secrets.append(_decrypt(settings, row))
        except ApiError:
            continue
    return tuple(secret for secret in secrets if secret)
