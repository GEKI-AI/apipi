from apipi.config import Settings

NO_COMPUTER_PROMPT = (
    "This session runs on ApiPi. There is no computer and no file or "
    "shell tools. Do not invent APIs or tools that this session does not "
    "provide."
)

HOSTED_PROMPT = (
    "This session runs on ApiPi. The working directory is /workspace. "
    "Write durable deliverables under outputs/ only. Those files are "
    "published when a turn completes and stay downloadable after the "
    "sandbox expires. Other files are scratch and are deleted with the "
    "workspace. Do not invent APIs or tools that this session does not "
    "provide."
)

SELF_HOSTED_PROMPT = (
    "This session runs on ApiPi. The working directory is the runner's "
    "files. Write durable deliverables under outputs/ only. Those files "
    "are published when a turn completes. Other files are scratch. Do not "
    "invent APIs or tools that this session does not provide."
)

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
    kind = _computer(env_type, chat)
    if kind == "hosted":
        return HOSTED_PROMPT
    if kind == "self_hosted":
        return SELF_HOSTED_PROMPT
    return NO_COMPUTER_PROMPT


def sandbox_size_hint(size: str | None, mem_mib: int | None) -> str:
    if not size or mem_mib is None:
        return ""
    return f"Sandbox size is {size} ({mem_mib} MiB)."


def network_hint(access: str | None) -> str:
    if access == "enabled":
        return "This sandbox has network access."
    if access == "restricted":
        return "This sandbox has restricted network access."
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
) -> str | None:
    if settings is None or settings.platform_prompt is None:
        main = _main_prompt(env_type, chat)
    else:
        main = settings.platform_prompt
    extra = "" if settings is None else settings.platform_prompt_additional
    size = ""
    net = ""
    if (
        _computer(env_type, chat) == "hosted"
        and settings is not None
        and settings.run_mode == "microvm"
    ):
        size = sandbox_size_hint(sandbox_size, mem_mib)
        net = network_hint(network)
    agent = agent_instructions or ""
    parts = [part for part in (main, extra, size, net, agent) if part]
    return "\n\n".join(parts) or None
