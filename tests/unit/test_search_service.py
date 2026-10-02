import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from apipi.config import ConfigError, Settings, load_settings
from apipi.services.search import (
    SearchResolver,
    SearchService,
    allowed_domains_of,
    web_search_tool,
)
from apipi.store.engine import Store
from apipi.store.models import utc_now
from apipi.store.repo import (
    append_event,
    create_agent,
    create_session,
    create_turn,
    ensure_tenant,
    get_session,
    search_usage_for_turn,
    set_session_lease,
    update_agent,
)

DB_URL = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"
TAVILY_BODY = {
    "results": [
        {"title": "One", "url": "https://one.example/a", "content": "first"},
        {"title": "Two", "url": "https://two.example/b", "content": "second"},
    ],
    "usage": {"credits": 1},
}


def _settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": DB_URL,
        "run_mode": "none",
        "search_provider": "tavily",
        "search_api_key": "secret-key",
    }
    base.update(values)
    return Settings(**base)


def _clear_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APIPI_RUN_MODE", raising=False)
    for name in (
        "APIPI_SEARCH_PROVIDER",
        "APIPI_SEARCH_API_KEY",
        "APIPI_SEARCH_BASE_URL",
        "APIPI_SEARCH_TIMEOUT",
        "APIPI_SEARCH_MAX_RESULTS",
        "APIPI_SEARCH_TAVILY_DEPTH",
        "APIPI_SEARCH_STAAN_MARKET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_search_settings_defaults() -> None:
    settings = Settings(database_url=DB_URL, run_mode="none")
    assert settings.search_provider is None
    assert settings.search_api_key is None
    assert settings.search_base_url is None
    assert settings.search_timeout == timedelta(seconds=15)
    assert settings.search_max_results == 5
    assert settings.search_tavily_depth == "basic"
    assert settings.search_staan_market == "en-us"


def test_search_provider_without_key_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APIPI_SEARCH_PROVIDER", "staan")
    with pytest.raises(ConfigError, match="APIPI_SEARCH_API_KEY"):
        load_settings()
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", "  ")
    with pytest.raises(ConfigError, match="APIPI_SEARCH_API_KEY"):
        load_settings()
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", "k")
    assert load_settings().search_provider == "staan"


def test_search_key_without_provider_is_fine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", "k")
    settings = load_settings()
    assert settings.search_provider is None


def test_search_unknown_provider_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APIPI_SEARCH_PROVIDER", "bing")
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", "k")
    with pytest.raises(ConfigError, match="APIPI_SEARCH_PROVIDER"):
        load_settings()


def test_search_toml_and_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_env(monkeypatch, tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        "[search]\n"
        'provider = "staan"\n'
        'base_url = "http://proxy.local"\n'
        'timeout = "7s"\n'
        "max_results = 8\n"
        'tavily_depth = "advanced"\n'
        'staan_market = "de-de"\n'
    )
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", "k")
    settings = load_settings()
    assert settings.search_provider == "staan"
    assert settings.search_base_url == "http://proxy.local"
    assert settings.search_timeout == timedelta(seconds=7)
    assert settings.search_max_results == 8
    assert settings.search_tavily_depth == "advanced"
    assert settings.search_staan_market == "de-de"
    monkeypatch.setenv("APIPI_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("APIPI_SEARCH_TIMEOUT", "3s")
    settings = load_settings()
    assert settings.search_provider == "tavily"
    assert settings.search_timeout == timedelta(seconds=3)


@pytest.mark.parametrize("key", ["api_key", "bogus"])
def test_search_toml_unknown_key_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, key: str
) -> None:
    _clear_env(monkeypatch, tmp_path)
    (tmp_path / "apipi.toml").write_text(
        'database_url = "postgresql://apipi:apipi@localhost:5432/apipi"\n'
        f'[search]\n{key} = "x"\n'
    )
    with pytest.raises(ConfigError, match=f"search.{key}"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("APIPI_SEARCH_TIMEOUT", "soon", "APIPI_SEARCH_TIMEOUT"),
        ("APIPI_SEARCH_TIMEOUT", "0s", "APIPI_SEARCH_TIMEOUT"),
        ("APIPI_SEARCH_MAX_RESULTS", "0", "APIPI_SEARCH_MAX_RESULTS"),
        ("APIPI_SEARCH_MAX_RESULTS", "21", "APIPI_SEARCH_MAX_RESULTS"),
        ("APIPI_SEARCH_TAVILY_DEPTH", "deep", "APIPI_SEARCH_TAVILY_DEPTH"),
        ("APIPI_SEARCH_BASE_URL", "ftp://x", "APIPI_SEARCH_BASE_URL"),
    ],
)
def test_search_invalid_values_fail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    value: str,
    message: str,
) -> None:
    _clear_env(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError, match=message):
        load_settings()


async def test_resolver_returns_global_config() -> None:
    settings = _settings(
        search_base_url="http://proxy.local",
        search_timeout=timedelta(seconds=9),
        search_max_results=7,
        search_tavily_depth="advanced",
        search_staan_market="de-de",
    )
    target = await SearchResolver(settings).resolve(uuid.uuid4(), "u", "o")
    assert target is not None
    assert target.provider == "tavily"
    assert target.credential == "secret-key"
    assert target.key_source == "operator"
    assert target.options.base_url == "http://proxy.local"
    assert target.options.timeout == 9.0
    assert target.options.max_results == 7
    assert target.options.tavily_depth == "advanced"
    assert target.options.staan_market == "de-de"


async def test_resolver_returns_none_without_provider() -> None:
    settings = Settings(database_url=DB_URL, run_mode="none")
    assert await SearchResolver(settings).resolve(uuid.uuid4(), None, None) is None


def test_tool_helpers() -> None:
    tool = {"type": "web_search", "filters": {"allowed_domains": ["a.com", "b.org"]}}
    assert web_search_tool([{"type": "function", "name": "x"}, tool]) is tool
    assert web_search_tool([{"type": "function"}]) is None
    assert web_search_tool(None) is None
    assert allowed_domains_of(tool) == ("a.com", "b.org")
    assert allowed_domains_of({"type": "web_search"}) == ()
    assert allowed_domains_of(None) == ()


class Case:
    def __init__(
        self,
        service: SearchService,
        store: Store,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        turn_id: uuid.UUID,
        worker_id: uuid.UUID,
        lease_id: uuid.UUID,
        requests: list[httpx.Request],
    ) -> None:
        self.service = service
        self.store = store
        self.tenant_id = tenant_id
        self.session_id = session_id
        self.turn_id = turn_id
        self.worker_id = worker_id
        self.lease_id = lease_id
        self.requests = requests

    def message(self, **values: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "type": "search.request",
            "request_id": str(uuid.uuid4()),
            "session_id": str(self.session_id),
            "turn_id": str(self.turn_id),
            "query": "pi agents",
            "max_results": None,
        }
        base.update(values)
        return base

    async def ask(self, **values: Any) -> dict[str, Any] | None:
        return await self.service.handle_request(
            self.message(**values),
            worker_id=self.worker_id,
            leases={self.lease_id},
        )

    async def usage(self) -> tuple[int, int, dict[str, dict[str, int]]]:
        async with self.store.session() as db:
            return await search_usage_for_turn(db, self.tenant_id, self.turn_id)


async def _case(
    store: Store,
    *,
    tools: list[dict[str, Any]] | None = None,
    inline: bool = False,
    status: str = "in_progress",
    turn_events: tuple[str, ...] = ("agent.session.turn.created",),
    handler: Any = None,
    settings: Settings | None = None,
) -> Case:
    tenant_id = uuid.uuid4()
    worker_id = uuid.uuid4()
    lease_id = uuid.uuid4()
    tool_list = tools if tools is not None else [{"type": "web_search"}]
    requests: list[httpx.Request] = []

    def default_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=TAVILY_BODY)

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(recording if handler else default_handler)
    )
    resolved = settings if settings is not None else _settings()
    service = SearchService(store, resolved, client=client)
    async with store.session() as db:
        await ensure_tenant(db, tenant_id)
        agent_id = None
        if not inline:
            agent = await create_agent(
                db, tenant_id, name="bot", model="test", tools=tool_list
            )
            agent_id = agent.id
        row = await create_session(
            db,
            tenant_id,
            agent_id=agent_id,
            status=status,
            environment={"type": "none"},
            tools=tool_list if inline else None,
            user_id="u1",
            org_id="o1",
        )
        session_id = row.id
        await set_session_lease(
            db,
            tenant_id,
            session_id,
            worker_id=worker_id,
            lease_id=lease_id,
            lease_until=utc_now() + timedelta(seconds=30),
        )
        turn = await create_turn(db, tenant_id, session_id, status="in_progress")
        for event_type in turn_events:
            await append_event(
                db,
                tenant_id,
                session_id,
                type=event_type,
                data={"turn_id": str(turn.id)},
            )
        turn_id = turn.id
    return Case(
        service, store, tenant_id, session_id, turn_id, worker_id, lease_id, requests
    )


async def test_search_ok_returns_results_and_records_usage(store: Store) -> None:
    case = await _case(store)
    reply = await case.ask(max_results=3)
    assert reply is not None
    assert reply["type"] == "search.reply"
    assert reply["ok"] is True
    assert reply["request_id"]
    assert [item["url"] for item in reply["results"]] == [
        "https://one.example/a",
        "https://two.example/b",
    ]
    assert reply["results"][0]["published_date"] is None
    body = json.loads(case.requests[0].content)
    assert body["max_results"] == 3
    assert "include_domains" not in body
    assert await case.usage() == (
        1,
        1,
        {"tavily/operator": {"calls": 1, "units": 1}},
    )
    await case.ask()
    assert (await case.usage())[:2] == (2, 2)


async def test_search_uses_allowed_domains_from_agent_not_from_worker(
    store: Store,
) -> None:
    case = await _case(
        store,
        tools=[{"type": "web_search", "filters": {"allowed_domains": ["a.com"]}}],
    )
    reply = await case.ask(allowed_domains=["evil.example"])
    assert reply is not None and reply["ok"] is True
    assert json.loads(case.requests[0].content)["include_domains"] == ["a.com"]


async def test_search_inline_session_tools(store: Store) -> None:
    case = await _case(store, inline=True)
    reply = await case.ask()
    assert reply is not None and reply["ok"] is True


async def test_search_max_results_is_capped_by_config(store: Store) -> None:
    case = await _case(store, settings=_settings(search_max_results=2))
    await case.ask(max_results=15)
    assert json.loads(case.requests[0].content)["max_results"] == 2
    await case.ask()
    assert json.loads(case.requests[1].content)["max_results"] == 2


async def test_search_denied_without_tool(store: Store) -> None:
    case = await _case(store, tools=[{"type": "function", "name": "x"}])
    reply = await case.ask()
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "search_denied"
    assert case.requests == []
    assert (await case.usage())[:2] == (0, 0)


async def test_search_denied_when_resolver_returns_none(store: Store) -> None:
    case = await _case(store, settings=Settings(database_url=DB_URL, run_mode="none"))
    reply = await case.ask()
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "search_denied"
    assert case.requests == []


async def test_search_denied_for_unleased_or_foreign_worker(store: Store) -> None:
    case = await _case(store)
    foreign = await case.service.handle_request(
        case.message(), worker_id=uuid.uuid4(), leases={case.lease_id}
    )
    assert foreign is not None and foreign["code"] == "search_denied"
    no_lease = await case.service.handle_request(
        case.message(), worker_id=case.worker_id, leases=set()
    )
    assert no_lease is not None and no_lease["code"] == "search_denied"
    unknown = await case.ask(session_id=str(uuid.uuid4()))
    assert unknown is not None and unknown["code"] == "search_denied"
    assert case.requests == []


async def test_search_denied_when_session_idle(store: Store) -> None:
    case = await _case(store, status="idle")
    reply = await case.ask()
    assert reply is not None and reply["code"] == "search_denied"


async def test_search_denied_when_turn_finished(store: Store) -> None:
    case = await _case(
        store,
        turn_events=("agent.session.turn.created", "agent.session.turn.completed"),
    )
    reply = await case.ask()
    assert reply is not None and reply["code"] == "search_denied"


async def test_search_denied_for_other_turn_id(store: Store) -> None:
    case = await _case(store)
    reply = await case.ask(turn_id=str(uuid.uuid4()))
    assert reply is not None and reply["code"] == "search_denied"
    assert case.requests == []


async def test_search_denied_after_agent_loses_tool(store: Store) -> None:
    case = await _case(store)
    async with store.session() as db:
        row = await get_session(db, case.tenant_id, case.session_id)
        assert row is not None and row.agent_id is not None
        await update_agent(db, case.tenant_id, row.agent_id, changes={"tools": []})
    reply = await case.ask()
    assert reply is not None and reply["code"] == "search_denied"


@pytest.mark.parametrize("query", ["", "   ", "x" * 2001])
async def test_search_invalid_query(store: Store, query: str) -> None:
    case = await _case(store)
    reply = await case.ask(query=query)
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "invalid_request"
    assert case.requests == []


async def test_search_unreadable_ids_get_no_reply(store: Store) -> None:
    case = await _case(store)
    assert (
        await case.service.handle_request(
            {"type": "search.request", "request_id": "x"},
            worker_id=case.worker_id,
            leases={case.lease_id},
        )
        is None
    )


async def test_search_bad_fields_reply_invalid_request(store: Store) -> None:
    case = await _case(store)
    reply = await case.ask(max_results=0)
    assert reply is not None and reply["code"] == "invalid_request"


async def test_provider_timeout_is_not_charged(store: Store) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    case = await _case(store, handler=handler)
    reply = await case.ask()
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "search_timeout"
    assert (await case.usage())[:2] == (0, 0)


@pytest.mark.parametrize(
    ("status", "code"), [(500, "search_unavailable"), (401, "search_failed")]
)
async def test_provider_http_error_is_not_charged(
    store: Store, status: int, code: str
) -> None:
    case = await _case(store, handler=lambda request: httpx.Response(status))
    reply = await case.ask()
    assert reply is not None
    assert reply["code"] == code
    assert "tavily" not in str(reply["message"]).lower()
    assert (await case.usage())[:2] == (0, 0)


async def test_charged_failure_is_counted(store: Store) -> None:
    case = await _case(
        store, handler=lambda request: httpx.Response(200, text="not json")
    )
    reply = await case.ask()
    assert reply is not None
    assert reply["ok"] is False
    assert reply["code"] == "search_failed"
    assert (await case.usage())[:2] == (1, 1)


async def test_staan_provider_through_service(store: Store) -> None:
    body = {
        "web": {"results": [{"title": "T", "url": "https://t.example", "snippet": "s"}]}
    }
    case = await _case(
        store,
        handler=lambda request: httpx.Response(200, json=body),
        settings=_settings(search_provider="staan", search_staan_market="de-de"),
    )
    reply = await case.ask()
    assert reply is not None and reply["ok"] is True
    assert reply["results"][0]["url"] == "https://t.example"
    assert case.requests[0].url.path == "/v2/search/web"
    assert await case.usage() == (1, 1, {"staan/operator": {"calls": 1, "units": 1}})


async def test_usage_write_failure_is_logged_not_raised(store: Store) -> None:
    case = await _case(store)
    case.turn_id = uuid.uuid4()
    reply = await case.service._record(
        case.tenant_id, case.session_id, case.turn_id, "tavily", "operator", 1, 1
    )
    assert reply is None
    assert (await case.usage())[:2] == (0, 0)


async def test_logs_never_hold_query_or_key(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    case = await _case(store)
    with caplog.at_level(logging.DEBUG):
        await case.ask(query="very secret question")
    text = " ".join(
        f"{record.getMessage()} {record.__dict__}" for record in caplog.records
    )
    assert "very secret question" not in text
    assert "secret-key" not in text
    events = [r for r in caplog.records if getattr(r, "event", "") == "search.request"]
    assert events
    assert events[0].__dict__["query_len"] == len("very secret question")
    assert events[0].__dict__["provider"] == "tavily"
