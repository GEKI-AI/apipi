import json
import shutil
import subprocess
from pathlib import Path

import httpx

from apipi.config import ConfigError, Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.dirs import sessions_root
from apipi.worker.pi.version import PINNED_PI

PI_PROVIDER = "apipi"
_listed: list[str] | None = None


def clear_model_cache() -> None:
    global _listed
    _listed = None


def remember_models(ids: list[str]) -> None:
    global _listed
    _listed = list(ids)


def _headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _unreachable() -> ApiError:
    return ApiError(
        "invalid_request",
        "Model host /models is unreachable",
        code="model_host_unreachable",
        status_code=400,
    )


def _unauthorized() -> ApiError:
    return ApiError(
        "invalid_request",
        "Model host rejected the API key",
        code="model_host_unauthorized",
        status_code=401,
    )


def _status_error(status: int) -> ApiError | None:
    if status in {401, 403}:
        return _unauthorized()
    if status >= 400:
        return _unreachable()
    return None


def models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def pi_agent_dir(settings: Settings) -> Path:
    path = sessions_root(settings) / ".pi" / "agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


def parse_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                ids.append(model_id)
    return ids


def fetch_model_ids(base_url: str, api_key: str | None = None) -> list[str]:
    try:
        response = httpx.get(
            models_url(base_url), headers=_headers(api_key), timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable") from exc
    if response.status_code in {401, 403}:
        return []
    if response.status_code >= 400:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable")
    try:
        payload = response.json()
    except ValueError as exc:
        raise ConfigError("OPENAI_BASE_URL /models is unreachable") from exc
    return parse_model_ids(payload)


async def fetch_models_json(base_url: str, api_key: str | None = None) -> object:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(models_url(base_url), headers=_headers(api_key))
    except httpx.HTTPError as exc:
        raise _unreachable() from exc
    error = _status_error(response.status_code)
    if error is not None:
        raise error
    try:
        return response.json()
    except ValueError as exc:
        raise _unreachable() from exc


async def listed_models(base_url: str, api_key: str | None = None) -> list[str]:
    return parse_model_ids(await fetch_models_json(base_url, api_key))


async def require_saved_model(
    settings: Settings, model: str | None, api_key: str | None = None
) -> None:
    if not isinstance(model, str) or not model.strip():
        return
    name = model.strip()
    if settings.model_list == "off":
        if settings.models:
            require_listed_model(name, list(settings.models))
        return
    if settings.model_list == "probe" and _listed is not None:
        require_listed_model(name, _listed)
        return
    if not settings.model_base_url:
        return
    ids = await listed_models(
        settings.model_base_url, api_key or settings.model_api_key_overwrite
    )
    if settings.model_list == "probe":
        remember_models(ids)
    require_listed_model(name, ids)


def require_model(model: str | None) -> str:
    if not isinstance(model, str) or not model.strip():
        raise ApiError(
            "invalid_request",
            "agent.model is required",
            code="model_required",
            status_code=400,
        )
    return model.strip()


def require_listed_model(model: str, ids: list[str]) -> None:
    if model not in ids:
        raise ApiError(
            "invalid_request",
            f"model {model} is not available on OPENAI_BASE_URL",
            code="model_not_found",
            status_code=400,
        )


def _thinking_level(settings: Settings, thinking: str | None) -> str:
    return thinking if thinking is not None else settings.pi_thinking


def _provider_compat(
    settings: Settings, thinking: str | None = None
) -> dict[str, bool]:
    return {
        "supportsDeveloperRole": False,
        "supportsReasoningEffort": _thinking_level(settings, thinking) != "off",
    }


def _model_row(
    model_id: str, settings: Settings, thinking: str | None = None
) -> dict[str, object]:
    row: dict[str, object] = {"id": model_id}
    if _thinking_level(settings, thinking) != "off":
        row["reasoning"] = True
    return row


def _apply_reasoning(provider: dict[str, object], *, enabled: bool) -> None:
    compat = provider.get("compat")
    if not isinstance(compat, dict):
        compat = {}
        provider["compat"] = compat
    compat["supportsReasoningEffort"] = enabled
    models = provider.get("models")
    if not isinstance(models, list):
        return
    for row in models:
        if not isinstance(row, dict):
            continue
        if enabled:
            row["reasoning"] = True
        else:
            row.pop("reasoning", None)


def note_pi_model(settings: Settings, model: str) -> None:
    path = pi_agent_dir(settings) / "models.json"
    if not path.is_file():
        write_pi_models_json(settings, [model])


def write_pi_models_json(settings: Settings, model_ids: list[str]) -> Path:
    base = settings.model_base_url
    if not base:
        raise ConfigError("OPENAI_BASE_URL is required")
    directory = pi_agent_dir(settings)
    payload = {
        "providers": {
            PI_PROVIDER: {
                "baseUrl": base,
                "api": "openai-completions",
                "apiKey": "$OPENAI_API_KEY",
                "compat": _provider_compat(settings),
                "models": [_model_row(model_id, settings) for model_id in model_ids],
            }
        }
    }
    path = directory / "models.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def models_json_for_base_url(
    settings: Settings, base_url: str, *, thinking: str | None = None
) -> bytes:
    enabled = _thinking_level(settings, thinking) != "off"
    path = pi_agent_dir(settings) / "models.json"
    if path.is_file():
        payload = json.loads(path.read_text())
        providers = payload.get("providers")
        if isinstance(providers, dict):
            provider = providers.get(PI_PROVIDER)
            if isinstance(provider, dict):
                provider["baseUrl"] = base_url
                _apply_reasoning(provider, enabled=enabled)
        return (json.dumps(payload, indent=2) + "\n").encode()
    return (
        json.dumps(
            {
                "providers": {
                    PI_PROVIDER: {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "$OPENAI_API_KEY",
                        "compat": _provider_compat(settings, thinking),
                        "models": [],
                    }
                }
            },
            indent=2,
        )
        + "\n"
    ).encode()


def pi_binary(settings: Settings) -> str:
    command = settings.pi_command.split()
    if not command:
        raise ConfigError("pi is not on PATH")
    return command[0]


def installed_pi_version(settings: Settings) -> str | None:
    command = settings.pi_command.split()
    if not command:
        return None
    binary = command[0]
    path = binary if "/" in binary else shutil.which(binary)
    if path is None:
        return None
    try:
        output = subprocess.check_output(
            [path, "--version"],
            text=True,
            timeout=10,
            stderr=subprocess.STDOUT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    version = output.strip().splitlines()[-1].strip() if output.strip() else ""
    return version or None


def require_pinned_pi(settings: Settings) -> None:
    version = installed_pi_version(settings)
    if version is None:
        raise ConfigError("pi is not on PATH")
    if version != PINNED_PI:
        raise ConfigError(f"pi version must be {PINNED_PI}")


def probe_model_host(settings: Settings) -> None:
    if not settings.model_base_url:
        raise ConfigError("OPENAI_BASE_URL is required")
    require_pinned_pi(settings)
    if settings.model_list == "turn":
        return
    if settings.model_list == "off":
        write_pi_models_json(settings, list(settings.models))
        return
    ids = fetch_model_ids(settings.model_base_url, settings.model_api_key_overwrite)
    remember_models(ids)
    write_pi_models_json(settings, ids)
