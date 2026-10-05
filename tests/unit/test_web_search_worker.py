import asyncio
import json
import tarfile
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from tests.support.fake_proc import FakeProc
from tests.support.waits import until

from apipi.config import Settings
from apipi.protocol import ContextAgent, SearchResultItem, parse_turn_context
from apipi.worker.client import run_worker, worker_outbox
from apipi.worker.execution import (
    LocalExecution,
    context_web_search,
    local_execution,
)
from apipi.worker.outbox import Outbox
from apipi.worker.pi.broker import (
    SNIPPET_LIMIT,
    SearchHookError,
    SessionBroker,
    format_search_results,
    start_broker,
)
from apipi.worker.pi.extension import (
    GUEST_WEB_SEARCH_EXTENSION,
    WEB_SEARCH_EXTENSION_REL,
    guest_extensions,
    host_mcp_extension,
    web_search_extension_source,
)
from apipi.worker.pi.harness import PiHarness
from apipi.worker.pi.map import map_pi_event
from apipi.worker.pi.microvm import env_file, guest_env, write_workspace_image
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import pi_command_args, pi_env


def _settings(tmp_path: Path | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions") if tmp_path else None,
    )


def _item(**kwargs: Any) -> SearchResultItem:
    base: dict[str, Any] = {"title": "Title", "url": "https://example.com/a"}
    base.update(kwargs)
    return SearchResultItem(**base)


async def _broker() -> SessionBroker:
    return await start_broker(
        _settings(), api_key="k", mcp_http=None, host="127.0.0.1", port=0
    )


def test_format_lists_title_url_snippet_and_date_when_known() -> None:
    text = format_search_results(
        [
            _item(title="First", snippet="About first", published_date="2026-01-02"),
            _item(title="Second", url="https://example.com/b"),
        ]
    )
    assert text == (
        "1. First\n"
        "   URL: https://example.com/a\n"
        "   Date: 2026-01-02\n"
        "   About first\n"
        "\n"
        "2. Second\n"
        "   URL: https://example.com/b"
    )


def test_format_truncates_and_flattens_untrusted_text() -> None:
    long = "word " * 400
    text = format_search_results(
        [_item(title="A\nB", snippet="line one\n\nline two\n" + long)]
    )
    lines = text.split("\n")
    assert lines[0] == "1. A B"
    snippet = lines[-1].strip()
    assert snippet.startswith("line one line two word")
    assert snippet.endswith("...")
    assert len(snippet) <= SNIPPET_LIMIT
    assert len(lines) == 3


def test_format_empty_results() -> None:
    assert format_search_results([]) == "No results."


async def test_broker_search_route_returns_text_and_passes_arguments() -> None:
    broker = await _broker()
    calls: list[tuple[str, str, str, int | None]] = []

    async def hook(
        session_id: str, turn_id: str, query: str, max_results: int | None
    ) -> dict[str, Any]:
        calls.append((session_id, turn_id, query, max_results))
        return {
            "ok": True,
            "results": [{"title": "T", "url": "https://x.test", "snippet": "S"}],
        }

    try:
        broker.set_context("sess-1", "agent-1")
        broker.set_turn("turn-1")
        broker.set_search(hook)
        assert broker.search_url.endswith(f"/{broker.token}/search")
        async with AsyncClient() as client:
            response = await client.post(
                broker.search_url, json={"query": "cats", "max_results": 3}
            )
            default = await client.post(
                broker.search_url, json={"query": "dogs", "max_results": None}
            )
        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "text": "1. T\n   URL: https://x.test\n   S",
        }
        assert default.json()["ok"] is True
        assert calls == [
            ("sess-1", "turn-1", "cats", 3),
            ("sess-1", "turn-1", "dogs", None),
        ]
    finally:
        await broker.stop()


async def test_broker_search_route_checks_token() -> None:
    broker = await _broker()
    called = False

    async def hook(*_args: object) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True, "results": []}

    try:
        broker.set_context("s", None)
        broker.set_turn("t")
        broker.set_search(hook)
        wrong = broker.search_url.replace(broker.token, "wrong-token")
        async with AsyncClient() as client:
            response = await client.post(wrong, json={"query": "q"})
        assert response.status_code == 404
        assert not called
    finally:
        await broker.stop()


async def test_broker_search_route_needs_active_turn_and_hook() -> None:
    broker = await _broker()

    async def hook(*_args: object) -> dict[str, Any]:
        return {"ok": True, "results": []}

    try:
        async with AsyncClient() as client:
            idle = await client.post(broker.search_url, json={"query": "q"})
            assert idle.status_code == 409
            assert idle.json()["ok"] is False
            broker.set_context("s", None)
            broker.set_search(hook)
            no_turn = await client.post(broker.search_url, json={"query": "q"})
            assert no_turn.status_code == 409
            broker.set_turn("t")
            active = await client.post(broker.search_url, json={"query": "q"})
            assert active.status_code == 200
            broker.clear_turn()
            after = await client.post(broker.search_url, json={"query": "q"})
            assert after.status_code == 409
            broker.set_turn("t2")
            no_hook = await client.post(broker.search_url, json={"query": "q"})
            assert no_hook.status_code == 409
    finally:
        await broker.stop()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 5},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": "3"},
        {"query": "q", "max_results": True},
        {"query": "x" * 2001},
    ],
)
async def test_broker_search_route_rejects_bad_body(body: dict[str, Any]) -> None:
    broker = await _broker()
    called = False

    async def hook(*_args: object) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True, "results": []}

    try:
        broker.set_context("s", None)
        broker.set_turn("t")
        broker.set_search(hook)
        async with AsyncClient() as client:
            response = await client.post(broker.search_url, json=body)
            garbage = await client.post(broker.search_url, content=b"not json")
        assert response.status_code == 400
        assert response.json()["ok"] is False
        assert garbage.status_code == 400
        assert not called
    finally:
        await broker.stop()


async def test_broker_search_route_turns_failures_into_tool_errors() -> None:
    broker = await _broker()
    mode = "denied"

    async def hook(*_args: object) -> dict[str, Any]:
        if mode == "denied":
            return {"ok": False, "code": "search_denied"}
        if mode == "message":
            return {"ok": False, "code": "search_failed", "message": "Provider down"}
        if mode == "hook_error":
            raise SearchHookError("search_unavailable", "Worker offline")
        if mode == "crash":
            raise RuntimeError("secret internals")
        if mode == "slow":
            await asyncio.sleep(5)
        if mode == "not_dict":
            return cast(Any, [])
        return {"ok": True, "results": [{"nope": 1}]}

    try:
        broker.set_context("s", None)
        broker.set_turn("t")
        broker.set_search(hook)
        broker.search_timeout = 0.1
        seen: dict[str, str] = {}
        async with AsyncClient() as client:
            for mode_name in (
                "denied",
                "message",
                "hook_error",
                "crash",
                "slow",
                "not_dict",
                "bad_results",
            ):
                mode = mode_name
                response = await client.post(broker.search_url, json={"query": "q"})
                body = response.json()
                assert body["ok"] is False, mode_name
                assert "text" not in body
                seen[mode_name] = body["error"]
        assert seen["denied"] == "Web search is not allowed for this session"
        assert seen["message"] == "Provider down"
        assert seen["hook_error"] == "Worker offline"
        assert seen["crash"] == "Web search failed"
        assert "secret" not in seen["crash"]
        assert seen["slow"] == "Web search timed out"
        assert seen["not_dict"] == "Web search failed"
        assert seen["bad_results"] == "Web search failed"
    finally:
        await broker.stop()


def test_pi_command_args_web_search_keeps_the_tool_enabled() -> None:
    settings = _settings()
    bare = pi_command_args(settings, tools=False)
    assert "--no-tools" in bare
    with_search = pi_command_args(settings, tools=False, web_search=True)
    assert "--no-tools" not in with_search
    assert "--no-builtin-tools" in with_search
    on = pi_command_args(settings, tools=True, web_search=True)
    assert "--no-tools" not in on
    assert "--no-builtin-tools" not in on
    code = pi_command_args(settings, tools=True, codemode="on", web_search=True)
    assert "--tools" not in code
    none_env = pi_command_args(settings, tools=True, env_type="none", web_search=True)
    assert "--no-builtin-tools" in none_env


def test_extension_is_loaded_only_with_the_flag(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    cwd = tmp_path / "ws"
    cwd.mkdir()
    off = host_mcp_extension(settings, str(cwd))
    on = host_mcp_extension(settings, str(cwd), web_search=True)
    assert [Path(item).name for item in off] == ["apipi.ts", "apipi-mcp.ts"]
    assert [Path(item).name for item in on][-1] == "apipi-web-search.ts"
    assert Path(on[-1]).read_bytes() == web_search_extension_source()
    args = pi_command_args(settings, tools=True, extension=on)
    assert args.count("--extension") == 3
    assert guest_extensions(False) == guest_extensions(True)[:-1]
    assert guest_extensions(True)[-1] == GUEST_WEB_SEARCH_EXTENSION


def test_extension_source_registers_the_tool() -> None:
    text = web_search_extension_source().decode()
    assert "registerTool" in text
    assert 'name: "web_search"' in text
    assert "APIPI_SEARCH_URL" in text
    assert "max_results" in text
    assert "throw new Error" in text


class _Broker:
    openai_base_url = "http://127.0.0.1:9/tok/v1"
    search_url = "http://127.0.0.1:9/tok/search"

    def mcp_url(self, route_id: str) -> str:
        return f"http://127.0.0.1:9/tok/mcp/{route_id}"


def test_pi_env_sets_search_url_only_with_flag_and_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    monkeypatch.setenv("APIPI_SEARCH_URL", "http://inherited.test/")
    extra = {"APIPI_SEARCH_URL": "http://evil.test/"}
    off = pi_env(settings, broker=_Broker(), extra_env=extra)
    assert "APIPI_SEARCH_URL" not in off
    on = pi_env(settings, broker=_Broker(), extra_env=extra, web_search=True)
    assert on["APIPI_SEARCH_URL"] == "http://127.0.0.1:9/tok/search"
    no_broker = pi_env(settings, extra_env=extra, web_search=True)
    assert "APIPI_SEARCH_URL" not in no_broker


SECRET = "tvly-super-secret-key"


def _search_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APIPI_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("APIPI_SEARCH_API_KEY", SECRET)
    monkeypatch.setenv("APIPI_SEARCH_BASE_URL", "https://search-proxy.test")
    monkeypatch.setenv("APIPI_SEARCH_STAAN_MARKET", "de-de")


def _no_provider(values: list[str]) -> None:
    joined = "\n".join(values).lower()
    assert SECRET not in joined
    assert "tavily" not in joined
    assert "staan" not in joined
    assert "search-proxy" not in joined


def test_pi_env_never_carries_provider_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _search_secrets(monkeypatch)
    for flag in (False, True):
        env = pi_env(_settings(), broker=_Broker(), web_search=flag)
        names = {key for key in env if key.startswith("APIPI_SEARCH_")}
        assert names <= {"APIPI_SEARCH_URL"}
        _no_provider(list(env.values()))


def test_guest_env_and_env_file_never_carry_provider_or_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _search_secrets(monkeypatch)
    for flag in (False, True):
        guest = guest_env(
            _settings(),
            broker=_Broker(),
            extra_env={"APIPI_SEARCH_API_KEY": SECRET, "APIPI_SEARCH_URL": "http://x/"},
            web_search=flag,
        )
        assert ("APIPI_SEARCH_URL" in guest) is flag
        if flag:
            assert guest["APIPI_SEARCH_URL"] == "http://127.0.0.1:9/tok/search"
        assert "APIPI_SEARCH_API_KEY" not in guest
        _no_provider(list(guest.values()))
        _no_provider([env_file(guest)])


def test_command_context_agent_holds_only_a_flag() -> None:
    fields = set(ContextAgent.model_fields)
    assert "web_search" in fields
    assert not {name for name in fields if "provider" in name or "key" in name}
    assert ContextAgent().web_search is False
    agent = ContextAgent(web_search=True)
    assert agent.model_dump()["web_search"] is True
    with pytest.raises(ValidationError):
        ContextAgent.model_validate({"web_search": "tavily"})
    with pytest.raises(ValidationError):
        ContextAgent.model_validate({"search_provider": "tavily"})
    with pytest.raises(ValidationError):
        ContextAgent.model_validate({"web_search": True, "search_api_key": SECRET})


def test_parse_turn_context_reads_the_flag() -> None:
    session = {"environment": {"type": "none"}}
    parsed = parse_turn_context({"session": session, "agent": {"web_search": True}})
    assert parsed.agent.web_search is True
    plain = parse_turn_context({"session": session})
    assert plain.agent.web_search is False
    dumped = json.dumps(parsed.model_dump())
    assert "tavily" not in dumped


def test_write_workspace_image_adds_extension_only_with_flag(tmp_path: Path) -> None:
    def names(flag: bool) -> list[str]:
        dest = tmp_path / f"ws-{flag}.tar"
        write_workspace_image(
            dest,
            cwd=None,
            env={"OPENAI_API_KEY": "k"},
            pi_args=["pi"],
            web_search=flag,
        )
        with tarfile.open(dest, mode="r") as tar:
            found = tar.getnames()
            if flag:
                member = tar.extractfile(WEB_SEARCH_EXTENSION_REL)
                assert member is not None
                assert member.read() == web_search_extension_source()
            return found

    assert WEB_SEARCH_EXTENSION_REL not in names(False)
    assert WEB_SEARCH_EXTENSION_REL in names(True)
    assert f"/workspace/{WEB_SEARCH_EXTENSION_REL}" == GUEST_WEB_SEARCH_EXTENSION


async def test_pool_respawns_when_web_search_flag_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[dict[str, Any]] = []

    async def fake_spawn(*_args: object, **kwargs: Any) -> FakeProc:
        spawned.append(kwargs)
        return FakeProc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    pool = PiPool(_settings())
    sid = uuid.uuid4()
    await pool.get(sid, cwd=None, tools=True)
    await pool.get(sid, cwd=None, tools=True, web_search=False)
    assert len(spawned) == 1
    assert "web_search" not in spawned[0]
    await pool.get(sid, cwd=None, tools=True, web_search=True)
    assert len(spawned) == 2
    assert spawned[1]["web_search"] is True
    await pool.get(sid, cwd=None, tools=True, web_search=True)
    assert len(spawned) == 2
    await pool.get(sid, cwd=None, tools=True)
    assert len(spawned) == 3
    assert "web_search" not in spawned[2]
    await pool.kill(sid)
    assert sid not in pool._web_search


class _HarnessBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def set_context(self, session_id: object, agent_id: object) -> None:
        self.calls.append(("context", session_id))

    def set_turn(self, turn_id: object) -> None:
        self.calls.append(("turn", turn_id))

    def set_search(self, hook: object) -> None:
        self.calls.append(("search", hook))

    def clear_turn(self) -> None:
        self.calls.append(("clear", None))


class _HarnessProc:
    stop_reason = None

    def __init__(self) -> None:
        self.broker = _HarnessBroker()

    async def prompt(self, text: str, images: object = None) -> Any:
        del text, images
        yield {"type": "agent_settled"}


class _HarnessPool:
    def __init__(self) -> None:
        self.proc = _HarnessProc()
        self.kwargs: dict[str, Any] = {}

    async def get(self, *_args: object, **kwargs: Any) -> _HarnessProc:
        self.kwargs = kwargs
        return self.proc

    def touch(self, session_id: object) -> None:
        del session_id


async def _drain(harness: PiHarness, **kwargs: Any) -> None:
    async for _event in harness.generate("hi", session_id=uuid.uuid4(), **kwargs):
        pass


async def test_harness_passes_flag_and_hook_to_pool_and_broker() -> None:
    pool = _HarnessPool()
    harness = PiHarness(cast(Any, pool))

    async def hook(*_args: object) -> dict[str, Any]:
        return {}

    await _drain(harness, turn_id="t1", web_search=True, search=hook)
    assert pool.kwargs["web_search"] is True
    assert ("search", hook) in pool.proc.broker.calls
    assert pool.proc.broker.calls[-1] == ("clear", None)

    await _drain(harness, turn_id="t2", search=hook)
    assert pool.kwargs["web_search"] is False
    assert ("search", None) in pool.proc.broker.calls


def _execution(tmp_path: Path) -> LocalExecution:
    settings = _settings(tmp_path)
    return local_execution(settings, outbox=Outbox(), harness=object())


async def test_execution_search_sends_request_and_returns_reply(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    sent: list[dict[str, Any]] = []
    session_id, turn_id = uuid.uuid4(), uuid.uuid4()

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)
        execution.handle_search_reply(
            {
                "type": "search.reply",
                "request_id": payload["request_id"],
                "session_id": payload["session_id"],
                "ok": True,
                "results": [],
            }
        )

    execution.search_sender = sender
    reply = await execution.search(str(session_id), str(turn_id), "cats", 4)
    assert reply["ok"] is True
    assert len(sent) == 1
    request = sent[0]
    assert request["type"] == "search.request"
    assert request["session_id"] == str(session_id)
    assert request["turn_id"] == str(turn_id)
    assert request["query"] == "cats"
    assert request["max_results"] == 4
    assert "provider" not in json.dumps(request)
    assert execution.search_waiters == {}


async def _silent_sender(payload: dict[str, Any]) -> None:
    del payload


async def _broken_sender(payload: dict[str, Any]) -> None:
    raise ConnectionError("socket closed")


@pytest.mark.parametrize(
    ("sender", "code"),
    [
        (None, "search_unavailable"),
        (_silent_sender, "search_timeout"),
        (_broken_sender, "search_unavailable"),
    ],
)
async def test_execution_search_failure_is_a_tool_error(
    tmp_path: Path, sender: Any, code: str
) -> None:
    execution = _execution(tmp_path)
    execution.search_timeout = 0.05
    execution.search_sender = sender
    with pytest.raises(SearchHookError) as raised:
        await execution.search(str(uuid.uuid4()), str(uuid.uuid4()), "q", None)
    assert raised.value.code == code
    assert execution.search_waiters == {}


async def test_execution_fail_search_waiters_and_stray_replies(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    started = asyncio.Event()

    async def sender(payload: dict[str, Any]) -> None:
        del payload
        started.set()

    execution.search_sender = sender
    task = asyncio.create_task(
        execution.search(str(uuid.uuid4()), str(uuid.uuid4()), "q", None)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    execution.handle_search_reply({"request_id": str(uuid.uuid4()), "ok": True})
    execution.handle_search_reply({"request_id": "not-a-uuid"})
    execution.handle_search_reply({})
    assert not task.done()
    execution.fail_search_waiters()
    with pytest.raises(SearchHookError):
        await asyncio.wait_for(task, timeout=1)
    assert execution.search_waiters == {}


def test_context_web_search_reads_the_agent_flag() -> None:
    assert context_web_search({"agent": {"web_search": True}}) is True
    assert context_web_search({"agent": {"web_search": False}}) is False
    assert context_web_search({"agent": {}}) is False
    assert context_web_search({"agent": {"web_search": "yes"}}) is False
    assert context_web_search({}) is False
    assert context_web_search(None) is False


async def test_execution_wraps_the_harness_only_for_search_turns(
    tmp_path: Path,
) -> None:
    seen: list[dict[str, Any]] = []

    class _Inner:
        def generate(self, *args: object, **kwargs: Any) -> str:
            seen.append(kwargs)
            return "stream"

        async def abort(self, session_id: object) -> str:
            return "aborted"

    inner = _Inner()
    execution = local_execution(_settings(tmp_path), outbox=Outbox(), harness=inner)
    assert execution._turn_harness({"agent": {}}) is inner
    wrapped = execution._turn_harness({"agent": {"web_search": True}})
    assert wrapped is not inner
    assert wrapped.generate("hi", session_id="s") == "stream"
    assert seen[0]["web_search"] is True
    assert seen[0]["search"] == execution.search
    assert seen[0]["session_id"] == "s"
    assert await wrapped.abort("s") == "aborted"


class _Sock:
    def __init__(self, label: str) -> None:
        self.label = label
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[Any] = asyncio.Queue()
        self.inbox.put_nowait(
            json.dumps(
                {
                    "ok": True,
                    "worker_id": str(uuid.uuid4()),
                    "lease_ttl_seconds": 30,
                    "heartbeat_seconds": 10,
                }
            )
        )

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    def requests(self) -> list[dict[str, Any]]:
        return [item for item in self.sent if item.get("type") == "search.request"]


class _Connect:
    def __init__(self, sockets: list[_Sock]) -> None:
        self.sockets = sockets

    def __call__(self, *_args: object, **_kwargs: object) -> "_Connect":
        self.sock = _Sock(f"sock-{len(self.sockets)}")
        self.sockets.append(self.sock)
        return self

    async def __aenter__(self) -> _Sock:
        return self.sock

    async def __aexit__(self, *_args: object) -> bool:
        return False


async def test_run_worker_routes_search_request_reply_and_fails_on_disconnect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "worker.token").write_text("secret\n")
    settings = Settings(
        run_mode="none",
        worker_token_file=str(tmp_path / "worker.token"),
        sessions_dir=str(tmp_path / "sessions"),
        worker_outbox_dir=str(tmp_path / "outbox"),
    )
    execution = local_execution(settings, outbox=worker_outbox(settings))

    async def idle() -> None:
        await asyncio.Event().wait()

    for loop_name in (
        "observe_loop",
        "reap_loop",
        "reap_workspace_loop",
        "sandbox_seen_loop",
    ):
        monkeypatch.setattr(execution, loop_name, idle)
    monkeypatch.setattr(
        "apipi.worker.execution.local_execution", lambda *_a, **_k: execution
    )
    monkeypatch.setattr(
        "apipi.worker.execution.worker_observability", lambda _s: (None, None)
    )
    monkeypatch.setattr("apipi.worker.client._install_drain_signals", lambda _e: None)
    monkeypatch.setattr("apipi.worker.client.asyncio.sleep", _fast_sleep)
    sockets: list[_Sock] = []
    connect = _Connect(sockets)

    task = asyncio.create_task(
        run_worker(settings, url="http://127.0.0.1:8000", connect=connect)
    )
    try:
        await until(lambda: execution.search_sender is not None)
        first = sockets[0]
        session_id, turn_id = uuid.uuid4(), uuid.uuid4()

        answered = asyncio.create_task(
            execution.search(str(session_id), str(turn_id), "cats", 2)
        )
        await until(lambda: len(first.requests()) == 1)
        request = first.requests()[0]
        assert request["query"] == "cats"
        assert request["max_results"] == 2
        assert request["session_id"] == str(session_id)
        first.inbox.put_nowait(
            json.dumps({"type": "search.reply", "request_id": str(uuid.uuid4())})
        )
        await asyncio.sleep(0.05)
        assert not answered.done()
        reply = {
            "type": "search.reply",
            "session_id": str(session_id),
            "request_id": request["request_id"],
            "ok": True,
            "results": [{"title": "T", "url": "https://x.test", "snippet": "S"}],
        }
        first.inbox.put_nowait(json.dumps(reply))
        assert await asyncio.wait_for(answered, timeout=2) == reply

        pending = asyncio.create_task(
            execution.search(str(session_id), str(turn_id), "lost", None)
        )
        await until(lambda: len(first.requests()) == 2)
        first.inbox.put_nowait(ConnectionResetError("socket lost"))
        with pytest.raises(SearchHookError) as raised:
            await asyncio.wait_for(pending, timeout=2)
        assert raised.value.code == "search_unavailable"
        assert execution.search_waiters == {}

        await until(lambda: len(sockets) == 2 and execution.search_sender is not None)
        await asyncio.sleep(0.1)
        assert sockets[1].requests() == []
        assert execution.search_waiters == {}
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert execution.search_sender is None


_real_sleep = asyncio.sleep


async def _fast_sleep(delay: float) -> None:
    await _real_sleep(min(delay, 0.01))


def test_map_web_search_tool_is_a_web_search_call() -> None:
    started = map_pi_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "web_search",
            "args": {"query": "cats", "max_results": 3},
        }
    )
    assert started == [
        (
            "agent.session.turn.item.added",
            {
                "item_type": "web_search_call",
                "call_id": "c1",
                "name": "web_search",
                "status": "in_progress",
                "action": {"type": "search", "query": "cats"},
            },
        )
    ]


def test_map_web_search_done_does_not_leak_result_text() -> None:
    done = map_pi_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "web_search",
            "isError": False,
            "result": {
                "content": [{"type": "text", "text": "1. Secret result\n   URL: x"}],
                "details": {"query": "cats"},
            },
        }
    )
    assert done[0][0] == "agent.session.turn.item.done"
    data = done[0][1]
    assert data == {
        "item_type": "web_search_call",
        "call_id": "c1",
        "name": "web_search",
        "is_error": False,
        "status": "completed",
        "action": {"type": "search", "query": "cats"},
    }
    assert "Secret" not in json.dumps(data)


def test_map_web_search_failure_carries_a_short_error() -> None:
    done = map_pi_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "web_search",
            "isError": True,
            "result": {
                "content": [
                    {"type": "text", "text": "Web search timed out\n" + "x" * 500}
                ],
                "details": {},
            },
        }
    )
    data = done[0][1]
    assert data["status"] == "failed"
    assert data["is_error"] is True
    assert data["error"].startswith("Web search timed out x")
    assert len(data["error"]) == 200
    assert data["action"] == {"type": "search"}
    empty = map_pi_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c2",
            "toolName": "web_search",
            "isError": True,
            "result": None,
        }
    )
    assert empty[0][1]["error"] == "Web search failed"


def test_map_web_search_is_not_command_or_mcp() -> None:
    for name in ("bash", "mcp_tavily_search", "mcp__docs__abcd1234"):
        started = map_pi_event(
            {"type": "tool_execution_start", "toolCallId": "c", "toolName": name}
        )
        assert started[0][1]["item_type"] != "web_search_call"
        assert "status" not in started[0][1]


async def test_execution_search_waits_until_earlier_events_are_acked(
    tmp_path: Path,
) -> None:
    execution = _execution(tmp_path)
    session_id, turn_id = uuid.uuid4(), uuid.uuid4()
    execution.outbox.append(session_id, "turn.status", {"status": "started"})
    sent: list[dict[str, Any]] = []

    async def sender(payload: dict[str, Any]) -> None:
        sent.append(payload)
        execution.handle_search_reply(
            {
                "type": "search.reply",
                "request_id": payload["request_id"],
                "session_id": payload["session_id"],
                "ok": True,
                "results": [],
            }
        )

    execution.search_sender = sender
    task = asyncio.create_task(
        execution.search(str(session_id), str(turn_id), "cats", None)
    )
    await asyncio.sleep(0.05)
    assert sent == []
    execution.outbox.acked(session_id, 1)
    reply = await asyncio.wait_for(task, timeout=2)
    assert reply["ok"] is True
    assert len(sent) == 1
