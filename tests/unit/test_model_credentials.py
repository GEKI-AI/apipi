import uuid
from pathlib import Path

import pytest

from apipi.common.errors import ApiError
from apipi.config import ConfigError, Settings
from apipi.gateway.auth import AuthIdentity
from apipi.services.model_credentials import ModelCredentials, load_model_credential

IDENTITY = AuthIdentity(key_id="k1", tenant_id=uuid.uuid4(), user_id="u1", org_id="o1")


def _settings(tmp_path: Path, overwrite: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_api_key_overwrite=overwrite,
    )


async def test_overwrite_wins_over_callback_and_bearer(tmp_path: Path) -> None:
    resolver = ModelCredentials(_settings(tmp_path, "operator"), lambda *_: "cb")
    assert await resolver.resolve(IDENTITY, "bearer") == "operator"


async def test_callback_wins_over_bearer_and_gets_the_identity(
    tmp_path: Path,
) -> None:
    seen: list[tuple[AuthIdentity, str | None]] = []

    def callback(identity: AuthIdentity, bearer: str | None) -> str:
        seen.append((identity, bearer))
        return "issued"

    resolver = ModelCredentials(_settings(tmp_path), callback)
    assert await resolver.resolve(IDENTITY, "bearer") == "issued"
    assert await resolver.resolve(IDENTITY, None) == "issued"
    assert seen == [(IDENTITY, "bearer"), (IDENTITY, None)]


async def test_async_callback(tmp_path: Path) -> None:
    async def callback(identity: AuthIdentity, bearer: str | None) -> str:
        return f"async:{identity.key_id}"

    resolver = ModelCredentials(_settings(tmp_path), callback)
    assert await resolver.resolve(IDENTITY, "bearer") == "async:k1"


async def test_bearer_is_used_without_overwrite_or_callback(tmp_path: Path) -> None:
    resolver = ModelCredentials(_settings(tmp_path))
    assert await resolver.resolve(IDENTITY, "bearer") == "bearer"


@pytest.mark.parametrize("bearer", [None, ""])
async def test_no_credential_fails_with_503(tmp_path: Path, bearer: str | None) -> None:
    resolver = ModelCredentials(_settings(tmp_path))
    with pytest.raises(ApiError) as caught:
        await resolver.resolve(IDENTITY, bearer)
    assert caught.value.code == "model_key_unavailable"
    assert caught.value.status_code == 503


@pytest.mark.parametrize("result", [None, "", 5])
async def test_empty_callback_result_fails_and_does_not_fall_back(
    tmp_path: Path, result: object
) -> None:
    resolver = ModelCredentials(_settings(tmp_path), lambda *_: result)
    with pytest.raises(ApiError) as caught:
        await resolver.resolve(IDENTITY, "bearer")
    assert caught.value.code == "model_key_unavailable"


async def test_raising_callback_does_not_leak_its_error(tmp_path: Path) -> None:
    def callback(identity: AuthIdentity, bearer: str | None) -> str:
        raise RuntimeError("secret-detail")

    resolver = ModelCredentials(_settings(tmp_path), callback)
    with pytest.raises(ApiError) as caught:
        await resolver.resolve(IDENTITY, "bearer")
    assert caught.value.code == "model_key_unavailable"
    assert "secret-detail" not in caught.value.message


def test_load_model_credential() -> None:
    assert load_model_credential(None) is None
    assert load_model_credential("") is None
    fn = load_model_credential("apipi.gateway.auth:authenticate")
    assert callable(fn)
    for bad in ("nocolon", "apipi.gateway.auth:missing", "missing_mod:fn", ":x"):
        with pytest.raises(ConfigError):
            load_model_credential(bad)


def test_example_token_round_trips_and_rejects_tampering() -> None:
    import importlib.util

    path = Path(__file__).resolve().parents[2] / "examples" / "model_credential.py"
    spec = importlib.util.spec_from_file_location("model_credential_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = module.model_credential(IDENTITY, "bearer")
    claims = module.verify_model_credential(token)
    assert claims is not None
    assert claims["key_id"] == "k1"
    assert claims["org_id"] == "o1"
    assert claims["user_id"] == "u1"
    assert claims["purpose"] == "model"
    assert "bearer" not in token
    assert module.verify_model_credential(token + "x") is None
