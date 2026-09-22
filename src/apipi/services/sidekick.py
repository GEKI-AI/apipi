import asyncio
import logging
import uuid
from typing import Any

import httpx

from apipi.config import Settings
from apipi.gateway.logutil import log_event

log = logging.getLogger("apipi")

SUMMARY_COMPLETED = "agent.session.turn.thinking.summary.completed"
SUMMARY_FAILED = "agent.session.turn.thinking.summary.failed"
SUMMARY_CHARS = 280
THINKING_SUMMARY_INPUT_CHARS = 3000
_TIMEOUT = 20.0
_tasks: set[asyncio.Task[None]] = set()

_PROMPT = (
    "Summarize the thinking below in one or two short sentences. "
    "Do not repeat it. Do not include secrets.\n\n"
)


class SidekickError(Exception):
    pass


def sidekick_key(settings: Settings, api_key: str | None) -> str | None:
    if isinstance(settings.sidekick_api_key, str) and settings.sidekick_api_key:
        return settings.sidekick_api_key
    if isinstance(api_key, str) and api_key:
        return api_key
    return None


def sidekick_base_url(settings: Settings) -> str | None:
    if isinstance(settings.sidekick_base_url, str) and settings.sidekick_base_url:
        return settings.sidekick_base_url.rstrip("/")
    if isinstance(settings.model_base_url, str) and settings.model_base_url:
        return settings.model_base_url.rstrip("/")
    return None


async def sidekick_complete(
    settings: Settings,
    *,
    api_key: str | None,
    prompt: str,
    temperature: float = 0,
) -> str:
    model = settings.sidekick_model
    if not isinstance(model, str) or not model.strip():
        raise SidekickError("sidekick model unset")
    base = sidekick_base_url(settings)
    key = sidekick_key(settings, api_key)
    if base is None or key is None:
        raise SidekickError("sidekick not configured")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{base}/chat/completions",
                json={
                    "model": model.strip(),
                    "temperature": temperature,
                    "max_tokens": 80,
                    "messages": [{"role": "user", "content": prompt}],
                },
                headers={"authorization": f"Bearer {key}"},
            )
    except httpx.HTTPError as exc:
        raise SidekickError("sidekick unreachable") from exc
    if response.status_code >= 400:
        raise SidekickError("sidekick status")
    return _completion_text(response.json())


def _completion_text(payload: object) -> str:
    if not isinstance(payload, dict):
        raise SidekickError("sidekick response")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SidekickError("sidekick response")
    first = choices[0]
    if not isinstance(first, dict):
        raise SidekickError("sidekick response")
    message = first.get("message")
    if not isinstance(message, dict):
        raise SidekickError("sidekick response")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SidekickError("sidekick response")
    text = content.strip()
    if len(text) <= SUMMARY_CHARS:
        return text
    return text[:SUMMARY_CHARS]


def schedule_thinking_summaries(
    store: Any,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    blocks: list[dict[str, str]],
    *,
    settings: Settings | None,
    api_key: str | None,
    enabled: bool,
) -> None:
    if settings is None or not settings.thinking_summary or not enabled:
        return
    pending = [
        block
        for block in blocks
        if isinstance(block.get("item_id"), str) and block.get("text", "").strip()
    ]
    if not pending:
        return
    try:
        task = asyncio.create_task(
            _summarize(
                store,
                hub,
                tenant_id,
                session_id,
                turn_id,
                pending,
                settings=settings,
                api_key=api_key,
            )
        )
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _summarize(
    store: Any,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    blocks: list[dict[str, str]],
    *,
    settings: Settings,
    api_key: str | None,
) -> None:
    from apipi.services.runtime import persist_event

    for block in blocks:
        item_id = block["item_id"]
        try:
            summary = await sidekick_complete(
                settings,
                api_key=api_key,
                prompt=_PROMPT + block["text"][:THINKING_SUMMARY_INPUT_CHARS],
            )
        except Exception:
            log_event(
                log,
                logging.WARNING,
                "thinking summary dropped",
                event="thinking.summary.dropped",
                error_code="summary_drop",
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
            )
            await _persist(
                persist_event,
                store,
                hub,
                tenant_id,
                session_id,
                type=SUMMARY_FAILED,
                data={
                    "item_id": item_id,
                    "turn_id": str(turn_id),
                    "summary_status": "failed",
                },
            )
            continue
        await _persist(
            persist_event,
            store,
            hub,
            tenant_id,
            session_id,
            type=SUMMARY_COMPLETED,
            data={
                "item_id": item_id,
                "turn_id": str(turn_id),
                "summary": summary,
                "summary_status": "done",
            },
        )


async def _persist(
    persist_event: Any,
    store: Any,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    type: str,
    data: dict[str, Any],
) -> None:
    try:
        async with store.session() as db:
            await persist_event(
                db,
                hub,
                tenant_id,
                session_id,
                type=type,
                data=data,
            )
    except Exception:
        log_event(
            log,
            logging.WARNING,
            "thinking summary dropped",
            event="thinking.summary.dropped",
            error_code="summary_drop",
            tenant_id=tenant_id,
            session_id=session_id,
        )
