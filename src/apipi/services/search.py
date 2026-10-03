import asyncio
import logging
import time
import uuid
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select

from apipi.common.errors import ApiError
from apipi.common.logutil import log_event
from apipi.config import Settings
from apipi.protocol import SearchReply, SearchRequest, SearchResultItem
from apipi.services.search_providers import (
    KeySource,
    SearchHit,
    SearchOptions,
    SearchProviderError,
    SearchQuery,
    SearchTarget,
    build_provider,
)
from apipi.store.engine import Store
from apipi.store.models import Event
from apipi.store.repo import get_agent, get_session_by_id, record_search_usage

log = logging.getLogger("apipi.search")

WEB_SEARCH_TOOL = "web_search"
MAX_ALLOWED_DOMAINS = 10
MAX_QUERY_CHARS = 2000
SEARCH_NOT_CONFIGURED = (
    "web_search is not available: no search provider is configured for this caller"
)

USAGE_WRITE_ATTEMPTS = 3
USAGE_WRITE_DELAY = 0.1

_TURN_EVENTS = (
    "agent.session.turn.created",
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
)

__all__ = [
    "SearchOptions",
    "SearchResolver",
    "SearchService",
    "SearchTarget",
    "allowed_domains_of",
    "require_search",
    "web_search_tool",
]


def web_search_tool(tools: object) -> dict[str, Any] | None:
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == WEB_SEARCH_TOOL:
            return tool
    return None


def allowed_domains_of(tool: dict[str, Any] | None) -> tuple[str, ...]:
    if tool is None:
        return ()
    filters = tool.get("filters")
    if not isinstance(filters, dict):
        return ()
    domains = filters.get("allowed_domains")
    if not isinstance(domains, list):
        return ()
    return tuple(item for item in domains if isinstance(item, str) and item)


class SearchResolver:
    """The one place that decides whether search is allowed and with what.

    The only implementation returns the global configuration for every
    tenant and subject. A later implementation can deny search per
    subject or return a tenant provider and key without any change to
    the worker protocol, the Pi tool or the agent shape.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def resolve(
        self,
        tenant_id: uuid.UUID,
        user_id: str | None,
        org_id: str | None,
    ) -> SearchTarget | None:
        settings = self._settings
        if settings.search_provider is None or not settings.search_api_key:
            return None
        return SearchTarget(
            provider=settings.search_provider,
            options=SearchOptions(
                base_url=settings.search_base_url,
                timeout=settings.search_timeout.total_seconds(),
                max_results=settings.search_max_results,
                tavily_depth=settings.search_tavily_depth,
                staan_market=settings.search_staan_market,
            ),
            credential=settings.search_api_key,
            key_source="operator",
        )


async def require_search(
    resolver: SearchResolver,
    tools: object,
    *,
    tenant_id: uuid.UUID,
    user_id: str | None,
    org_id: str | None,
) -> None:
    if web_search_tool(tools) is None:
        return
    if await resolver.resolve(tenant_id, user_id, org_id) is None:
        raise ApiError(
            "invalid_request",
            SEARCH_NOT_CONFIGURED,
            code="search_not_configured",
            status_code=400,
        )


def _failure(
    session_id: uuid.UUID, request_id: uuid.UUID, code: str, message: str
) -> dict[str, Any]:
    return SearchReply(
        session_id=session_id,
        request_id=request_id,
        ok=False,
        results=[],
        code=code,
        message=message,
    ).to_wire()


def _success(
    session_id: uuid.UUID, request_id: uuid.UUID, hits: list[SearchHit]
) -> dict[str, Any]:
    return SearchReply(
        session_id=session_id,
        request_id=request_id,
        ok=True,
        results=[
            SearchResultItem(
                title=hit.title,
                url=hit.url,
                snippet=hit.snippet,
                published_date=hit.published_date,
            )
            for hit in hits
        ],
        code=None,
        message=None,
    ).to_wire()


class SearchService:
    """Answers `search.request` messages from workers.

    Each request is checked against the database, not against what the
    worker says: the session must be leased to the asking worker, the
    turn must be running, the effective agent must carry the
    `web_search` tool, and the resolver must still allow search for the
    session's tenant and subject. The provider call goes through the
    shared HTTP client without redirects. Usage is counted here, in the
    same place as the call: a call counts when it succeeded or when the
    provider charged for it anyway. Timeouts, transport errors and HTTP
    errors are not charged.
    """

    def __init__(
        self,
        store: Store,
        settings: Settings,
        resolver: SearchResolver | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.resolver = resolver if resolver is not None else SearchResolver(settings)
        self._client = client
        self._owns_client = client is None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def handle_request(
        self,
        message: dict[str, Any],
        *,
        worker_id: uuid.UUID,
        leases: set[uuid.UUID],
    ) -> dict[str, Any] | None:
        """Return a `search.reply` dict, or None when the ids cannot be read."""
        try:
            session_id = uuid.UUID(str(message.get("session_id")))
            request_id = uuid.UUID(str(message.get("request_id")))
        except ValueError:
            return None
        try:
            request = SearchRequest.model_validate(message)
        except ValidationError:
            return _failure(
                session_id, request_id, "invalid_request", "invalid search request"
            )
        query = request.query.strip()
        if not query or len(query) > MAX_QUERY_CHARS:
            return _failure(
                session_id,
                request_id,
                "invalid_request",
                f"query must be 1 to {MAX_QUERY_CHARS} characters",
            )
        started = time.monotonic()
        try:
            return await self._handle(
                request, query, worker_id=worker_id, leases=leases, started=started
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log_event(
                log,
                logging.ERROR,
                "search request failed",
                event="search.request",
                error_code="search_failed",
                exc_info=True,
                session_id=str(session_id),
                query_len=len(query),
                status="error",
                latency_ms=_ms(started),
            )
            return _failure(session_id, request_id, "search_failed", "search failed")

    async def _handle(
        self,
        request: SearchRequest,
        query: str,
        *,
        worker_id: uuid.UUID,
        leases: set[uuid.UUID],
        started: float,
    ) -> dict[str, Any]:
        session_id = request.session_id
        request_id = request.request_id

        def denied(reason: str) -> dict[str, Any]:
            log_event(
                log,
                logging.WARNING,
                "search denied",
                event="search.denied",
                error_code="search_denied",
                session_id=str(session_id),
                reason=reason,
            )
            return _failure(
                session_id,
                request_id,
                "search_denied",
                "web_search is not allowed for this session",
            )

        async with self.store.session() as db:
            row = await get_session_by_id(db, session_id)
            if (
                row is None
                or row.worker_id != worker_id
                or row.lease_id is None
                or row.lease_id not in leases
            ):
                return denied("not_leased")
            tenant_id = row.tenant_id
            user_id = row.user_id
            org_id = row.org_id
            if row.status != "in_progress":
                return denied("turn_not_running")
            if not await _turn_running(db, tenant_id, session_id, request.turn_id):
                return denied("turn_not_running")
            if row.agent_id is not None:
                agent = await get_agent(db, tenant_id, row.agent_id)
                tools = agent.tools if agent is not None else None
            else:
                tools = row.tools
        tool = web_search_tool(tools)
        if tool is None:
            return denied("tool_missing")
        target = await self.resolver.resolve(tenant_id, user_id, org_id)
        if target is None:
            return denied("resolver")
        cap = target.options.max_results
        wanted = request.max_results if request.max_results is not None else cap
        search_query = SearchQuery(
            query=query,
            max_results=max(1, min(wanted, cap)),
            allowed_domains=allowed_domains_of(tool),
        )
        provider = build_provider(target, self._http())
        calls = 0
        units = 0
        failure: SearchProviderError | None = None
        hits: list[SearchHit] = []
        try:
            response = await provider.search(search_query)
        except SearchProviderError as exc:
            failure = exc
            if exc.charged:
                calls, units = 1, exc.units
        else:
            hits = response.results
            calls, units = 1, response.units
        if calls:
            await self._record(
                tenant_id,
                session_id,
                request.turn_id,
                target.provider,
                target.key_source,
                calls,
                units,
            )
        log_event(
            log,
            logging.INFO if failure is None else logging.WARNING,
            "search call",
            event="search.request",
            error_code=failure.code if failure is not None else None,
            session_id=str(session_id),
            tenant_id=str(tenant_id),
            provider=target.provider,
            key_source=target.key_source,
            query_len=len(query),
            status="ok" if failure is None else failure.code,
            units=units,
            results=len(hits),
            latency_ms=_ms(started),
        )
        if failure is not None:
            return _failure(session_id, request_id, failure.code, failure.message)
        return _success(session_id, request_id, hits)

    async def _record(
        self,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        provider: str,
        key_source: KeySource,
        calls: int,
        units: int,
    ) -> None:
        for attempt in range(USAGE_WRITE_ATTEMPTS):
            try:
                async with self.store.session() as db:
                    await record_search_usage(
                        db,
                        tenant_id,
                        session_id,
                        turn_id,
                        provider=provider,
                        key_source=key_source,
                        calls=calls,
                        units=units,
                    )
                return
            except Exception:
                if attempt + 1 < USAGE_WRITE_ATTEMPTS:
                    await asyncio.sleep(USAGE_WRITE_DELAY * (attempt + 1))
                    continue
                log_event(
                    log,
                    logging.ERROR,
                    "search usage not recorded",
                    event="search.usage_failed",
                    error_code="usage_write",
                    exc_info=True,
                    session_id=str(session_id),
                    provider=provider,
                    key_source=key_source,
                    calls=calls,
                    units=units,
                )


async def _turn_running(
    db: Any, tenant_id: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID
) -> bool:
    rows = await db.scalars(
        select(Event)
        .where(
            Event.tenant_id == tenant_id,
            Event.session_id == session_id,
            Event.type.in_(_TURN_EVENTS),
        )
        .order_by(Event.seq.desc())
        .limit(20)
    )
    wanted = str(turn_id)
    for event in rows:
        data = event.data
        if isinstance(data, dict) and data.get("turn_id") == wanted:
            return event.type == "agent.session.turn.created"
    return False


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
