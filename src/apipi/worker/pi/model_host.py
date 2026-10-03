import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from apipi.common.dirs import sessions_root
from apipi.common.models import fetch_model_ids, remember_models
from apipi.config import ConfigError, Settings
from apipi.worker.pi.version import PINNED_PI

PI_PROVIDER = "apipi"


def pi_agent_dir(settings: Settings) -> Path:
    path = sessions_root(settings) / ".pi" / "agent"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    from apipi.common.model_caps import apply_capability, registry_of

    row: dict[str, object] = {"id": model_id}
    apply_capability(
        row,
        registry_of(settings.model_registry).get(model_id),
        reasoning=_thinking_level(settings, thinking) != "off",
    )
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
                "models": _merge_registry_models(
                    settings,
                    [_model_row(model_id, settings) for model_id in model_ids],
                    thinking=None,
                ),
            }
        }
    }
    path = directory / "models.json"
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _merge_registry_models(
    settings: Settings, models: list[Any], *, thinking: str | None
) -> list[Any]:
    from apipi.common.model_caps import registry_of

    seen: set[str] = set()
    out: list[object] = []
    for row in models:
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            seen.add(row["id"])
            out.append(_model_row(row["id"], settings, thinking))
        else:
            out.append(row)
    for model_id in registry_of(settings.model_registry):
        if model_id not in seen:
            out.append(_model_row(model_id, settings, thinking))
    return out


def models_json_for_base_url(
    settings: Settings,
    base_url: str,
    *,
    thinking: str | None = None,
    model: str | None = None,
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
                models = provider.get("models")
                if not isinstance(models, list):
                    models = []
                if (
                    isinstance(model, str)
                    and model
                    and model
                    not in {row.get("id") for row in models if isinstance(row, dict)}
                ):
                    models.append({"id": model})
                provider["models"] = _merge_registry_models(
                    settings, models, thinking=thinking
                )
        return (json.dumps(payload, indent=2) + "\n").encode()
    models: list[Any] = []
    if isinstance(model, str) and model:
        models.append({"id": model})
    return (
        json.dumps(
            {
                "providers": {
                    PI_PROVIDER: {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "$OPENAI_API_KEY",
                        "compat": _provider_compat(settings, thinking),
                        "models": _merge_registry_models(
                            settings, models, thinking=thinking
                        ),
                    }
                }
            },
            indent=2,
        )
        + "\n"
    ).encode()


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
