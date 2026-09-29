from apipi.config import Settings

_HOSTED = frozenset({"openai_hosted", "hosted"})


def _computer(env_type: str | None, chat: bool) -> str | None:
    if chat or not env_type or env_type == "none":
        return None
    if env_type == "self_hosted":
        return "self_hosted"
    if env_type in _HOSTED:
        return "hosted"
    return None


def _main_prompt(env_type: str | None, chat: bool) -> str:
    from apipi.worker.pi.fragments import render_shipped

    kind = _computer(env_type, chat) or "none"
    return render_shipped(
        f"main.{kind}",
        {
            "platform_name": "ApiPi",
            "workspace": "/workspace" if kind == "hosted" else "",
        },
    )


def sandbox_size_hint(size: str | None, mem_mib: int | None) -> str:
    if not size or mem_mib is None:
        return ""
    from apipi.worker.pi.fragments import render_shipped

    return render_shipped("size", {"size": size, "mem_mib": str(mem_mib)})


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
    from pathlib import Path

    from apipi.worker.pi.fragments import fragment_source, fragment_text

    overrides: list[str] = []
    if fragment_source(settings, "size")[0] is not None:
        overrides.append(fragment_text(settings, "size", values, strict=False))
    if fragment_source(settings, "network")[0] is not None:
        overrides.append(fragment_text(settings, "network", values, strict=False))
    directory = Path(cwd) / ".pi" / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    import json

    (directory / "capability.json").write_text(
        json.dumps({"block": block, "overrides": [item for item in overrides if item]})
        + "\n"
    )


def network_hint(access: str | None) -> str:
    from apipi.worker.pi.fragments import render_shipped

    if access == "enabled":
        return render_shipped("network.enabled", {})
    if access == "restricted":
        return render_shipped("network.restricted", {})
    return ""


def compose_instructions(
    settings: Settings | None,
    agent_instructions: str | None,
    *,
    env_type: str | None = None,
    chat: bool = False,
    sandbox_size: str | None = None,
    mem_mib: int | None = None,
    network: str | None = None,
    idle_ttl: str | None = None,
    image: str | None = None,
    vcpus: int | None = None,
    cwd: str | None = None,
) -> str | None:
    from apipi.worker.pi.fragments import cap_prompt, fragment_text

    kind = _computer(env_type, chat) or "none"
    if settings is None:
        main = _main_prompt(env_type, chat)
        extra = ""
        size = ""
        net = ""
    else:
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
        size = ""
        net = ""
        if kind == "hosted" and settings.run_mode == "microvm":
            from apipi.worker.pi.fragments import capability_block, fragment_source

            _write_capability(cwd, capability_block(settings, values), settings, values)
            if fragment_source(settings, "size")[0] is not None:
                size = fragment_text(settings, "size", values, strict=False)
            if fragment_source(settings, "network")[0] is not None:
                net = fragment_text(settings, "network", values, strict=False)
    agent = agent_instructions or ""
    parts = [part for part in (main, extra, size, net, agent) if part]
    text = "\n\n".join(parts)
    return cap_prompt(text) if text else None
