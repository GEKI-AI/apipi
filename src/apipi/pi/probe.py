import asyncio

from apipi.config import Settings


async def _probe(settings: Settings) -> None:
    if settings.run_mode == "jail":
        from apipi.pi.jail import probe_jail

        await probe_jail(settings)
        return
    if settings.run_mode == "microvm":
        from apipi.pi.microvm import probe_microvm

        await probe_microvm(settings)


def probe_run_mode(settings: Settings) -> None:
    if settings.run_mode == "host":
        return
    asyncio.run(_probe(settings))
