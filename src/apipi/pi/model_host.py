import json
import shutil
import subprocess
from pathlib import Path

import httpx

from apipi.config import ConfigError, Settings
from apipi.errors import ApiError
from apipi.pi.dirs import sessions_root
from apipi.pi.version import PINNED_PI

PI_PROVIDER = "apipi"


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
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(models_url(base_url), headers=headers, timeout=10.0)
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


def fetch_models_json(base_url: str, api_key: str | None = None) -> object:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = httpx.get(models_url(base_url), headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise ApiError(
            "invalid_request",
            "Model host /models is unreachable",
            code="model_host_unreachable",
            status_code=400,
        ) from exc
    if response.status_code in {401, 403}:
        raise ApiError(
            "invalid_request",
            "Model host rejected the API key",
            code="model_host_unauthorized",
            status_code=401,
        )
    if response.status_code >= 400:
        raise ApiError(
            "invalid_request",
            "Model host /models is unreachable",
            code="model_host_unreachable",
            status_code=400,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(
            "invalid_request",
            "Model host /models is unreachable",
            code="model_host_unreachable",
            status_code=400,
        ) from exc


def listed_models(base_url: str, api_key: str | None = None) -> list[str]:
    return parse_model_ids(fetch_models_json(base_url, api_key))


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
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                },
                "models": [{"id": model_id} for model_id in model_ids],
            }
        }
    }
    path = directory / "models.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def models_json_for_base_url(settings: Settings, base_url: str) -> bytes:
    path = pi_agent_dir(settings) / "models.json"
    if path.is_file():
        payload = json.loads(path.read_text())
        providers = payload.get("providers")
        if isinstance(providers, dict):
            provider = providers.get(PI_PROVIDER)
            if isinstance(provider, dict):
                provider["baseUrl"] = base_url
        return (json.dumps(payload, indent=2) + "\n").encode()
    return (
        json.dumps(
            {
                "providers": {
                    PI_PROVIDER: {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "$OPENAI_API_KEY",
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                        },
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
    ids = fetch_model_ids(settings.model_base_url, settings.model_api_key_overwrite)
    write_pi_models_json(settings, ids)
