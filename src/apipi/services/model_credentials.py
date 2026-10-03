import importlib
from collections.abc import Callable

from apipi.common.errors import ApiError
from apipi.config import ConfigError, Settings
from apipi.gateway.auth import AuthIdentity, call_off_loop

ModelCredential = Callable[..., object]

MODEL_KEY_UNAVAILABLE = "No model credential is available for this request"


def model_key_unavailable() -> ApiError:
    return ApiError(
        "api_error",
        MODEL_KEY_UNAVAILABLE,
        code="model_key_unavailable",
        status_code=503,
    )


def load_model_credential(path: str | None) -> ModelCredential | None:
    if path is None or path == "":
        return None
    message = "APIPI_MODEL_CREDENTIAL must be package.mod:func"
    if ":" not in path:
        raise ConfigError(message)
    module_name, func_name = path.rsplit(":", 1)
    if not module_name or not func_name:
        raise ConfigError(message)
    try:
        fn = getattr(importlib.import_module(module_name), func_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(message) from exc
    if not callable(fn):
        raise ConfigError(message)
    return fn


class ModelCredentials:
    """The one place that decides which key a turn sends to the model host.

    The order is the operator key, then the callback, then the request
    bearer. When none gives a value the request fails.
    """

    def __init__(
        self, settings: Settings, callback: ModelCredential | None = None
    ) -> None:
        self.settings = settings
        self.callback = callback

    async def resolve(self, identity: AuthIdentity, bearer: str | None) -> str:
        overwrite = self.settings.model_api_key_overwrite
        if isinstance(overwrite, str) and overwrite:
            return overwrite
        if self.callback is not None:
            try:
                value = await call_off_loop(self.callback, identity, bearer)
            except Exception as exc:
                raise model_key_unavailable() from exc
            if not isinstance(value, str) or not value:
                raise model_key_unavailable()
            return value
        if isinstance(bearer, str) and bearer:
            return bearer
        raise model_key_unavailable()
