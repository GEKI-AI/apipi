import importlib

from apipi.config import ConfigError
from apipi.worker.pi.isolation.base import Isolation

_cache: dict[str, Isolation] = {}


def load_isolation(mode: str) -> Isolation:
    cached = _cache.get(mode)
    if cached is not None:
        return cached
    backend = _resolve(mode)
    _cache[mode] = backend
    return backend


def isolation_name(mode: str) -> str:
    return load_isolation(mode).name


def _resolve(mode: str) -> Isolation:
    if mode == "host":
        raise ConfigError("APIPI_RUN_MODE=host is not valid")
    if mode == "jail":
        raise ConfigError("APIPI_RUN_MODE=jail is not valid")
    if mode == "none":
        from apipi.worker.pi.isolation.none import NoneIsolation

        return NoneIsolation()
    if mode == "chat":
        from apipi.worker.pi.isolation.chat import ChatIsolation

        return ChatIsolation()
    if mode == "microvm":
        from apipi.worker.pi.isolation.microvm import MicrovmIsolation

        return MicrovmIsolation()
    if ":" not in mode:
        raise ConfigError(
            "APIPI_RUN_MODE must be none, chat, microvm, or package.mod:Class"
        )
    return _load_custom(mode)


def _load_custom(path: str) -> Isolation:
    module_name, attr_name = path.rsplit(":", 1)
    if not module_name or not attr_name:
        raise ConfigError(
            "APIPI_RUN_MODE must be none, chat, microvm, or package.mod:Class"
        )
    try:
        module = importlib.import_module(module_name)
        attr = getattr(module, attr_name)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"APIPI_RUN_MODE backend not found: {path}") from exc
    backend = attr() if isinstance(attr, type) or callable(attr) else attr
    for item in (
        "name",
        "needs_probe",
        "stdio_on_host",
        "warn_not_production",
        "require",
        "probe",
        "spawn",
    ):
        if not hasattr(backend, item):
            raise ConfigError(f"APIPI_RUN_MODE backend missing {item}: {path}")
    return backend
