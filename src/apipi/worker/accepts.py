from apipi.config import ConfigError, Settings

ACCEPT_KINDS = frozenset({"none", "microvm"})


def resolved_worker_accepts(settings: Settings) -> frozenset[str]:
    explicit = settings.worker_accepts
    if explicit is not None:
        return frozenset(explicit)
    if settings.run_mode == "microvm":
        return frozenset({"none", "microvm"})
    try:
        from apipi.worker.pi.isolation import load_isolation

        if load_isolation(settings.run_mode).name == "microvm":
            return frozenset({"none", "microvm"})
    except ConfigError:
        pass
    return frozenset({"none"})


def require_worker_accepts(settings: Settings) -> frozenset[str]:
    accepts = resolved_worker_accepts(settings)
    if not accepts or not accepts <= ACCEPT_KINDS:
        from apipi.config import WORKER_ACCEPTS_HELP

        raise ConfigError(WORKER_ACCEPTS_HELP)
    if "microvm" in accepts:
        if settings.run_mode != "microvm" and ":" not in settings.run_mode:
            raise ConfigError(
                "APIPI_WORKER_ACCEPTS includes microvm but "
                f"APIPI_RUN_MODE={settings.run_mode} cannot run microVM sessions"
            )
        if ":" in settings.run_mode:
            from apipi.worker.pi.isolation import load_isolation

            load_isolation(settings.run_mode).require(settings)
        else:
            from apipi.worker.pi.microvm import require_microvm

            require_microvm(settings)
    return accepts
