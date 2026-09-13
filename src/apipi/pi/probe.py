import asyncio

from apipi.config import Settings
from apipi.pi.isolation import load_isolation


def probe_run_mode(settings: Settings) -> None:
    backend = load_isolation(settings.run_mode)
    if not backend.needs_probe:
        return
    asyncio.run(backend.probe(settings))
