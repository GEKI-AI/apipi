from apipi.config import Settings

_HOSTED = frozenset({"openai_hosted"})


def _computer(env_type: str | None) -> str | None:
    if not env_type or env_type == "none":
        return None
    if env_type in _HOSTED:
        return "hosted"
    return None


def _idle_label(settings: Settings) -> str:
    seconds = int(settings.idle_ttl.total_seconds())
    if seconds % 3600 == 0 and seconds:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0 and seconds:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _write_capability(
    cwd: str | None,
    block: str,
    settings: Settings,
    values: dict[str, str],
) -> None:
    if not cwd:
        return
    import json
    from pathlib import Path

    directory = Path(cwd) / ".pi" / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "capability.json").write_text(
        json.dumps({"block": block, "overrides": []}) + "\n"
    )


def compose_instructions(
    settings: Settings | None,
    agent_instructions: str | None,
    *,
    env_type: str | None = None,
    sandbox_size: str | None = None,
    mem_mib: int | None = None,
    network: str | None = None,
    idle_ttl: str | None = None,
    image: str | None = None,
    vcpus: int | None = None,
    cwd: str | None = None,
) -> str | None:
    from apipi.worker.pi.fragments import (
        cap_prompt,
        capability_block,
        fragment_text,
    )

    if settings is None:
        from apipi.config import ConfigError

        raise ConfigError("compose_instructions needs settings")
    kind = _computer(env_type) or "none"
    ttl = idle_ttl or _idle_label(settings)
    values = {
        "platform_name": settings.platform_name or "ApiPi",
        "env_type": kind,
        "workspace": "/workspace" if kind == "hosted" else "",
        "size": sandbox_size or "",
        "mem_mib": "" if mem_mib is None else str(mem_mib),
        "vcpus": "" if vcpus is None else str(vcpus),
        "image": image or "",
        "network": network or "",
        "idle_ttl": ttl,
        "date": "",
        "has_browser": "",
    }
    main = fragment_text(settings, f"main.{kind}", values, strict=False)
    extra = fragment_text(settings, f"additional.{kind}", values, strict=False)
    if kind == "hosted" and settings.run_mode == "microvm":
        _write_capability(cwd, capability_block(settings, values), settings, values)
    agent = agent_instructions or ""
    parts = [part for part in (main, extra, agent) if part]
    text = "\n\n".join(parts)
    return cap_prompt(text) if text else None
