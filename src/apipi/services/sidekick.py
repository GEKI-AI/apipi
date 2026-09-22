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
TITLE_UPDATED = "agent.session.title.updated"
TITLE_KEY = "apipi.title"
TITLE_STATUS_KEY = "apipi.title_status"
SUMMARY_CHARS = 280
THINKING_SUMMARY_INPUT_CHARS = 3000
TITLE_CHARS = 60
_TIMEOUT = 20.0
_tasks: set[asyncio.Task[None]] = set()

_PROMPT = (
    "Summarize the thinking below in one or two short sentences. "
    "Do not repeat it. Do not include secrets.\n\n"
)
_TITLE_PROMPT = (
    "Write a short session title of at most 60 characters. No quotes. One line.\n\n"
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


def schedule_auto_title(
    store: Any,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    settings: Settings | None,
    api_key: str | None,
    enabled: bool,
    text: str | None,
) -> None:
    if settings is None or not settings.auto_title or not enabled:
        return
    try:
        task = asyncio.create_task(
            _title(
                store,
                hub,
                tenant_id,
                session_id,
                settings=settings,
                api_key=api_key,
                text=text,
            )
        )
    except RuntimeError:
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _title(
    store: Any,
    hub: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    settings: Settings,
    api_key: str | None,
    text: str | None,
) -> None:
    from apipi.services.runtime import persist_event
    from apipi.store.repo import get_session, list_items, update_session

    user_text = text.strip() if isinstance(text, str) else ""
    try:
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None or _has_title(row.metadata_json):
                return
            if not user_text:
                user_text = _first_user_text(
                    await list_items(db, tenant_id, session_id)
                )
            if not user_text:
                return
            await _merge_title(
                update_session,
                db,
                tenant_id,
                session_id,
                row.metadata_json,
                status="pending",
                title=None,
            )
    except Exception:
        _drop_title(tenant_id, session_id)
        return
    try:
        raw = await sidekick_complete(
            settings,
            api_key=api_key,
            prompt=_TITLE_PROMPT + user_text,
        )
        title = _clean_title(raw)
        if not title:
            raise SidekickError("sidekick response")
        status = "done"
    except Exception:
        _drop_title(tenant_id, session_id)
        title = None
        status = "failed"
    try:
        async with store.session() as db:
            row = await get_session(db, tenant_id, session_id)
            if row is None or _has_title(row.metadata_json):
                return
            await _merge_title(
                update_session,
                db,
                tenant_id,
                session_id,
                row.metadata_json,
                status=status,
                title=title,
            )
            data: dict[str, Any] = {"title_status": status}
            if title:
                data["title"] = title
            await persist_event(
                db,
                hub,
                tenant_id,
                session_id,
                type=TITLE_UPDATED,
                data=data,
            )
    except Exception:
        _drop_title(tenant_id, session_id)


def _has_title(metadata: object) -> bool:
    if not isinstance(metadata, dict):
        return False
    title = metadata.get(TITLE_KEY)
    return isinstance(title, str) and bool(title.strip())


def _first_user_text(items: object) -> str:
    if not isinstance(items, list):
        return ""
    for item in items:
        data = getattr(item, "data", None)
        if not isinstance(data, dict) or data.get("role") != "user":
            continue
        content = data.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return ""


def _clean_title(raw: str) -> str:
    line = raw.strip().split("\n")[0].strip().strip("\"'")
    if len(line) <= TITLE_CHARS:
        return line.strip()
    return line[:TITLE_CHARS].rstrip()


async def _merge_title(
    update_session: Any,
    db: Any,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    metadata: object,
    *,
    status: str,
    title: str | None,
) -> None:
    merged = dict(metadata) if isinstance(metadata, dict) else {}
    if title:
        merged[TITLE_KEY] = title
    merged[TITLE_STATUS_KEY] = status
    await update_session(db, tenant_id, session_id, changes={"metadata": merged})


def _drop_title(tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    log_event(
        log,
        logging.WARNING,
        "session title dropped",
        event="session.title.dropped",
        error_code="title_drop",
        tenant_id=tenant_id,
        session_id=session_id,
    )
