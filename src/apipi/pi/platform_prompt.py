from apipi.config import Settings

DEFAULT_PLATFORM_PROMPT = (
    "This session runs on ApiPi.\n"
    "\n"
    "When a computer is present, the working directory is /workspace on a "
    "hosted sandbox, or the runner's files for self_hosted. Write durable "
    "deliverables under outputs/ only. Those files are published when a "
    "turn completes and stay downloadable after the sandbox expires. Other "
    "files are scratch and are deleted with the workspace.\n"
    "\n"
    "When environment type is none, there is no computer and no file or "
    "shell tools.\n"
    "\n"
    "Do not invent APIs or tools that this session does not provide."
)


def compose_instructions(
    settings: Settings | None, agent_instructions: str | None
) -> str | None:
    if settings is None or settings.platform_prompt is None:
        main = DEFAULT_PLATFORM_PROMPT
    else:
        main = settings.platform_prompt
    extra = "" if settings is None else settings.platform_prompt_additional
    agent = agent_instructions or ""
    parts = [part for part in (main, extra, agent) if part]
    return "\n\n".join(parts) or None
