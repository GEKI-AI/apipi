import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from tests.support import fake_sink
from tests.support.http import MockClient
from tests.support.prom import metric_line

from apipi.common.metrics import Metrics
from apipi.config import Settings
from apipi.services.lifecycle_export import (
    LifecycleEmitter,
    api_heartbeat_loop,
    backoff_seconds,
    create_lifecycle,
    reconcile,
    shape_user_id,
)
from apipi.store.engine import Store
from apipi.worker.pi.microvm import resolve_spawn_image
from apipi.worker.pi.pool import PiPool
from apipi.worker.pi.proc import PiProc

_DB = "postgresql+asyncpg://apipi:apipi@localhost:5432/apipi"


class _Proc:
    def __init__(self, image: object | None = None) -> None:
        self.alive = True
        self.vm_id = None
        self.image = image
        self.process = type("P", (), {"pid": 7})()

    async def terminate(self) -> None:
        self.alive = False


class _Image:
    def __init__(self, image_id: str, version: str, digest: str) -> None:
        self.id = image_id
        self.version = version
        self.digest = digest


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": _DB,
        "run_mode": "none",
        "lifecycle_export_url": "http://export.test/life",
        "lifecycle_batch_wait": timedelta(milliseconds=20),
        "lifecycle_retry_max": timedelta(milliseconds=5),
    }
    values.update(updates)
    return Settings.model_validate(values)


def _pool(settings: Settings, metrics: Metrics | None = None) -> PiPool:
    pool = PiPool(settings, metrics=metrics)
    pool.lifecycle = create_lifecycle(settings, metrics)
    return pool


async def _spawn(
    pool: PiPool,
    monkeypatch: pytest.MonkeyPatch,
    *,
    image: object | None = None,
    session_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    key_id: str | None = None,
    env_type: str | None = None,
    instructions: str | None = None,
    mem_mib: int | None = None,
) -> uuid.UUID:
    async def fake_spawn(*_args: object, **_kw: object) -> _Proc:
        return _Proc(image)

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    sid = session_id or uuid.uuid4()
    await pool.get(
        sid,
        cwd=None,
        tools=True,
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        org_id=org_id,
        key_id=key_id,
        env_type=env_type,
        instructions=instructions,
        mem_mib=mem_mib,
    )
    return sid


def _events(pool: PiPool) -> list[dict[str, Any]]:
    emitter = pool.lifecycle
    assert emitter is not None
    return emitter.pending()


async def test_off_by_default_builds_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(database_url=_DB, run_mode="none")
    pool = _pool(settings)
    assert create_lifecycle(settings) is None
    assert pool.lifecycle is None

    async def fake_spawn(*_args: object, **_kw: object) -> _Proc:
        return _Proc()

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    sid = uuid.uuid4()
    await pool.get(sid, cwd=None, tools=True, agent_id="a", user_id="u")
    await pool.kill(sid)
    assert pool._live == {}


def test_run_modes_filter_uses_each_events_run_mode() -> None:
    settings = _settings(run_mode="none", lifecycle_run_modes="microvm")
    emitter = create_lifecycle(settings)
    assert emitter is not None
    assert (
        emitter.emit_start({"session_id": "a", "run_mode": "none"}, cause="spawn")
        is None
    )
    assert emitter.emit_start({"session_id": "b"}, cause="spawn") is None
    assert (
        emitter.emit_stop(
            {"session_id": "a", "run_mode": "none"}, reason="stop", live_ms=1
        )
        is None
    )
    assert emitter.pending() == []
    seq = emitter.emit_start({"session_id": "c", "run_mode": "microvm"}, cause="spawn")
    assert seq == 1
    stop = emitter.emit_stop(
        {"session_id": "c", "run_mode": "microvm", "start_seq": 1},
        reason="stop",
        live_ms=1,
    )
    assert stop == 2
    emitter.emit_heartbeat(
        [
            {"session_id": "a", "run_mode": "none"},
            {"session_id": "c", "run_mode": "microvm"},
        ]
    )
    events = emitter.pending()
    assert [item["type"] for item in events] == [
        "session.live.start",
        "session.live.stop",
        "session.live.heartbeat",
    ]
    assert [item["run_mode"] for item in events[:2]] == ["microvm", "microvm"]
    assert [item["session_id"] for item in events[2]["live"]] == ["c"]


def test_event_run_mode_comes_from_the_event_not_the_api() -> None:
    emitter = LifecycleEmitter(_settings(run_mode="none"))
    emitter.emit_start({"session_id": "a", "run_mode": "microvm"}, cause="spawn")
    emitter.emit_start({"session_id": "b"}, cause="spawn")
    events = emitter.pending()
    assert events[0]["run_mode"] == "microvm"
    assert events[1]["run_mode"] is None


def test_user_id_hash_and_omit() -> None:
    assert shape_user_id("ada@ex.com", "omit", None) is None
    hashed = shape_user_id("ada@ex.com", "hash", "secret")
    assert hashed is not None
    assert hashed != "ada@ex.com"
    assert shape_user_id("ada@ex.com", "hash", "secret") == hashed
    assert shape_user_id("ada@ex.com", "raw", "secret") == "ada@ex.com"


def test_hash_requires_key() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="APIPI_LIFECYCLE_USER_ID_KEY"):
        Settings(
            database_url=_DB,
            run_mode="none",
            lifecycle_user_id="hash",
        )


def test_heartbeat_off_values() -> None:
    off = Settings(database_url=_DB, run_mode="none", lifecycle_heartbeat="off")
    zero = Settings(database_url=_DB, run_mode="none", lifecycle_heartbeat="0")
    assert off.lifecycle_heartbeat is None
    assert zero.lifecycle_heartbeat is None


async def test_start_once_and_reuse_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _pool(_settings())
    sid = await _spawn(
        pool,
        monkeypatch,
        tenant_id=uuid.uuid4(),
        agent_id="agent-1",
        user_id="user-1",
        org_id="acme",
        key_id="key-1",
        env_type="none",
    )
    await pool.get(
        sid,
        cwd=None,
        tools=True,
        tenant_id=uuid.uuid4(),
        agent_id="agent-1",
        user_id="user-1",
        org_id="acme",
        key_id="key-1",
        env_type="none",
    )
    events = _events(pool)
    assert len(events) == 1
    start = events[0]
    assert start["type"] == "session.live.start"
    assert start["cause"] == "spawn"
    assert start["schema_version"] == 1
    assert start["seq"] == 1
    assert start["event_id"] == f"{start['boot_id']}:{start['seq']}"
    assert start["agent_id"] == "agent-1"
    assert start["user_id"] == "user-1"
    assert start["org_id"] == "acme"
    assert start["key_id"] == "key-1"
    assert start["environment_type"] == "none"
    assert start["sandbox_image"] is None
    assert start["image_version"] is None
    assert start["image_digest"] is None
    assert start["sandbox_size"]
    assert start["worker_id"] is None
    assert "tag" not in start
    assert "reason" not in start


async def test_every_stop_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = Metrics()
    settings = _settings(pi_mem_mib=1, idle_ttl=timedelta(milliseconds=1))
    pool = _pool(settings, metrics)

    async def reasons() -> list[str]:
        found: list[str] = []

        idle = await _spawn(pool, monkeypatch, env_type="none")
        pool._last[idle] = time.monotonic() - 10
        await pool.reap()
        found.append(_events(pool)[-1]["reason"])

        stopped = await _spawn(pool, monkeypatch, env_type="none")
        await pool.kill(stopped)
        found.append(_events(pool)[-1]["reason"])

        changed = await _spawn(pool, monkeypatch, env_type="none", instructions="a")
        await pool.get(changed, cwd=None, tools=True, instructions="b", env_type="none")
        kinds = [item["type"] for item in _events(pool)]
        assert kinds[-2:] == ["session.live.stop", "session.live.start"]
        assert _events(pool)[-2]["reason"] == "respawn"
        assert _events(pool)[-1]["cause"] == "respawn"
        found.append("respawn")

        memory = await _spawn(pool, monkeypatch, env_type="none")
        proc = pool._procs[memory]
        monkeypatch.setattr(
            "apipi.worker.procmem.read_group_rss_pss",
            lambda _pid: (10 * 1024 * 1024, 0),
        )
        await pool.enforce_memory()
        found.append(_events(pool)[-1]["reason"])
        assert proc.alive is False

        crashed = await _spawn(pool, monkeypatch, env_type="none")
        cast(Any, pool._procs[crashed]).alive = False
        await pool.get(crashed, cwd=None, tools=True, env_type="none")
        assert _events(pool)[-2]["reason"] == "crash"
        found.append("crash")

        swept = await _spawn(pool, monkeypatch, env_type="none")
        cast(Any, pool._procs[swept]).alive = False
        await pool.sweep_dead()
        found.append(_events(pool)[-1]["reason"])

        await _spawn(pool, monkeypatch, env_type="none")
        await pool.kill_unheld(reason="drain")
        found.append(_events(pool)[-1]["reason"])

        await _spawn(pool, monkeypatch, env_type="none")
        await pool.close()
        found.append(_events(pool)[-1]["reason"])
        return found

    assert await reasons() == [
        "idle",
        "stop",
        "respawn",
        "memory",
        "crash",
        "crash",
        "drain",
        "shutdown",
    ]
    body = metrics.scrape().decode()
    assert 'reason="crash"' in body
    assert 'reason="drain"' in body
    assert 'reason="memory"' in body


async def test_seq_is_monotonic_and_live_ms_uses_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _pool(_settings())
    sid = await _spawn(pool, monkeypatch, env_type="none")
    pool._born[sid] = time.monotonic() - 1.5
    pool._live[sid]["born"] = pool._born[sid]
    await pool.kill(sid)
    events = _events(pool)
    assert [item["seq"] for item in events] == [1, 2]
    assert events[1]["start_seq"] == 1
    assert events[1]["event_id"] == f"{events[1]['boot_id']}:2"
    assert events[1]["live_ms"] >= 1400


async def test_environment_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    pulled = _Image("browser", "0.61.0-3f9a2c1d", "9b1c")
    legacy = _Image("default", "legacy", "legacy")
    micro = _pool(_settings(run_mode="microvm"))
    pulled_id = await _spawn(
        micro,
        monkeypatch,
        image=pulled,
        env_type="openai_hosted",
        mem_mib=2048,
    )
    legacy_id = await _spawn(
        micro,
        monkeypatch,
        image=legacy,
        env_type="openai_hosted",
        mem_mib=512,
    )
    none_pool = _pool(_settings(run_mode="none"))
    none_id = await _spawn(
        none_pool, monkeypatch, image=pulled, env_type="none", mem_mib=512
    )
    assert micro.lifecycle is not None
    assert none_pool.lifecycle is not None
    micro.lifecycle.emit_heartbeat(micro.live_entries())
    none_pool.lifecycle.emit_heartbeat(none_pool.live_entries())

    def fields(event: dict[str, Any]) -> tuple[object, ...]:
        return (
            event["environment_type"],
            event["sandbox_image"],
            event["image_version"],
            event["image_digest"],
            event["sandbox_size"],
        )

    pulled_start = _events(micro)[0]
    legacy_start = _events(micro)[1]
    assert fields(pulled_start)[1:4] == ("browser", "0.61.0-3f9a2c1d", "9b1c")
    assert pulled_start["environment_type"] == "openai_hosted"
    assert fields(legacy_start)[1:4] == ("default", "legacy", "legacy")
    beat = _events(micro)[-1]
    assert beat["type"] == "session.live.heartbeat"
    by_id = {item["session_id"]: item for item in beat["live"]}
    assert fields(by_id[str(pulled_id)])[1:4] == ("browser", "0.61.0-3f9a2c1d", "9b1c")
    assert fields(by_id[str(legacy_id)])[1:4] == ("default", "legacy", "legacy")
    none_start = _events(none_pool)[0]
    assert fields(none_start)[1:4] == (None, None, None)
    assert none_start["sandbox_size"]
    none_beat = _events(none_pool)[-1]["live"]
    assert fields(none_beat[0])[1:4] == (None, None, None)
    assert none_beat[0]["session_id"] == str(none_id)
    assert "tag" not in pulled_start
    assert "tag" not in beat["live"][0]


def test_resolve_image_captures_current_and_ignores_later_flip(tmp_path) -> None:
    images = tmp_path / "images"
    first = images / "browser" / "0.1.0-aaaa"
    second = images / "browser" / "0.2.0-bbbb"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "rootfs.ext4").write_bytes(b"one")
    (second / "rootfs.ext4").write_bytes(b"two")
    (first / "manifest.json").write_text(
        json.dumps({"rootfs": {"sha256": "digest-one"}})
    )
    (second / "manifest.json").write_text(
        json.dumps({"rootfs": {"sha256": "digest-two"}})
    )
    (images / "browser" / "current").write_text("0.1.0-aaaa\n")
    settings = Settings(database_url=_DB, run_mode="microvm", images_dir=str(images))
    rootfs = str(first / "rootfs.ext4")
    captured = resolve_spawn_image(settings, "browser", rootfs)
    (images / "browser" / "current").write_text("0.2.0-bbbb\n")
    assert captured.version == "0.1.0-aaaa"
    assert captured.digest == "digest-one"
    flipped = resolve_spawn_image(settings, "browser", str(second / "rootfs.ext4"))
    assert flipped.version == "0.2.0-bbbb"
    assert flipped.digest == "digest-two"
    legacy = resolve_spawn_image(settings, "browser", str(tmp_path / "rootfs.ext4"))
    assert legacy.version is None
    assert legacy.digest is None


async def test_spawn_capture_survives_image_flip(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    images = tmp_path / "images"
    first = images / "browser" / "v1"
    second = images / "browser" / "v2"
    first.mkdir(parents=True)
    second.mkdir()
    (first / "rootfs.ext4").write_bytes(b"one")
    (second / "rootfs.ext4").write_bytes(b"two")
    (first / "manifest.json").write_text(json.dumps({"rootfs": {"sha256": "d1"}}))
    (second / "manifest.json").write_text(json.dumps({"rootfs": {"sha256": "d2"}}))
    current = images / "browser" / "current"
    current.write_text("v1\n")
    settings = _settings(run_mode="microvm", images_dir=str(images))
    pool = _pool(settings)

    async def fake_spawn(*_args: object, **kwargs: object) -> _Proc:
        selected = kwargs.get("image")
        image_id = selected if isinstance(selected, str) else "browser"
        version = current.read_text().strip()
        rootfs = images / image_id / version / "rootfs.ext4"
        resolved = resolve_spawn_image(settings, image_id, str(rootfs))
        return _Proc(resolved)

    monkeypatch.setattr("apipi.worker.pi.pool.spawn_pi", fake_spawn)
    sid = uuid.uuid4()
    await pool.get(sid, cwd=None, tools=True, image="browser", env_type="openai_hosted")
    current.write_text("v2\n")
    assert pool.lifecycle is not None
    pool.lifecycle.emit_heartbeat(pool.live_entries())
    assert _events(pool)[-1]["live"][0]["image_version"] == "v1"
    assert _events(pool)[-1]["live"][0]["image_digest"] == "d1"
    await pool.get(
        sid,
        cwd=None,
        tools=True,
        image="browser",
        env_type="openai_hosted",
        instructions="changed",
    )
    assert _events(pool)[-2]["image_version"] == "v1"
    assert _events(pool)[-1]["cause"] == "respawn"
    assert _events(pool)[-1]["image_version"] == "v2"
    assert _events(pool)[-1]["image_digest"] == "d2"


async def test_unbound_proc_never_starts_or_heartbeats() -> None:
    pool = _pool(_settings())
    sid = uuid.uuid4()
    pool._procs[sid] = cast(PiProc, _Proc())
    assert pool.live_entries() == []
    assert pool.lifecycle is not None
    pool.lifecycle.emit_heartbeat(pool.live_entries())
    events = _events(pool)
    assert events[0]["type"] == "session.live.heartbeat"
    assert events[0]["live"] == []
    assert all(item["type"] != "session.live.start" for item in events)


async def test_heartbeat_fake_clock(store: Store) -> None:
    settings = _settings(lifecycle_heartbeat=timedelta(seconds=60))
    emitter = create_lifecycle(settings)
    assert emitter is not None
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    async def sleep(seconds: float) -> None:
        clock["t"] += seconds

    class _Hub:
        def known_live_sessions(self) -> list[uuid.UUID]:
            return []

    await api_heartbeat_loop(
        settings, emitter, _Hub(), store, sleep=sleep, clock=now, max_emits=2
    )
    events = emitter.pending()
    assert [item["type"] for item in events] == [
        "session.live.heartbeat",
        "session.live.heartbeat",
    ]
    assert events[0]["live"] == []
    assert events[0]["interval_s"] == 60
    assert events[1]["seq"] == events[0]["seq"] + 1
    disabled = LifecycleEmitter(
        Settings(database_url=_DB, run_mode="none", lifecycle_heartbeat="off")
    )
    assert disabled.heartbeat_s is None


async def test_hooks_do_not_wait_on_hanging_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def handle(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        ready.set()
        await stop.wait()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    sockets = server.sockets or []
    port = sockets[0].getsockname()[1]
    settings = _settings(
        lifecycle_export_url=f"http://127.0.0.1:{port}/life",
        lifecycle_export_timeout=timedelta(seconds=2),
        lifecycle_batch_wait=timedelta(0),
        lifecycle_retry_max=timedelta(milliseconds=20),
    )
    pool = _pool(settings)
    assert pool.lifecycle is not None
    pool.lifecycle.start()
    task = pool.lifecycle._task
    try:
        sid = await _spawn(pool, monkeypatch, env_type="none")
        await asyncio.wait_for(ready.wait(), timeout=2)
        started = time.monotonic()
        await pool.kill(sid)
        other = await _spawn(pool, monkeypatch, env_type="none")
        elapsed = time.monotonic() - started
        assert elapsed < 0.4
        assert other
    finally:
        stop.set()
        if task is not None:
            task.cancel()
        server.close()
        await server.wait_closed()


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.content:
            captured.append(json.loads(request.content))
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    monkeypatch.setattr(
        "apipi.services.lifecycle_export.httpx.AsyncClient",
        lambda **_kwargs: MockClient(transport),
    )
    return captured


async def test_retry_keeps_order_and_drops_other_4xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        if calls["n"] == 2:
            return httpx.Response(429)
        return httpx.Response(204)

    captured = _patch_client(monkeypatch, handler)
    metrics = Metrics()
    pool = _pool(_settings(lifecycle_batch_wait=timedelta(0)), metrics)
    assert pool.lifecycle is not None
    pool.lifecycle.start()
    sid = await _spawn(pool, monkeypatch, env_type="none")
    await pool.lifecycle.flush(1)
    assert captured
    assert captured[0]["events"][0]["session_id"] == str(sid)
    assert all(item["events"][0]["session_id"] == str(sid) for item in captured[:3])
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_lifecycle_export_total", result="retry")
    assert metric_line(body, "apipi_lifecycle_export_total", result="ok")

    def drop(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    captured.clear()
    _patch_client(monkeypatch, drop)
    await pool.kill(sid)
    await pool.lifecycle.flush(1)
    body = metrics.scrape().decode()
    assert metric_line(body, "apipi_lifecycle_export_total", result="drop")


async def test_batching_flushes_on_size_and_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _patch_client(monkeypatch, lambda _request: httpx.Response(204))
    settings = _settings(
        lifecycle_batch=2,
        lifecycle_batch_wait=timedelta(milliseconds=30),
    )
    emitter = LifecycleEmitter(settings)
    emitter.start()
    emitter.emit_start({"session_id": "a", "sandbox_size": "S"}, cause="spawn")
    emitter.emit_start({"session_id": "b", "sandbox_size": "S"}, cause="spawn")
    emitter.emit_start({"session_id": "c", "sandbox_size": "S"}, cause="spawn")
    await emitter.flush(1)
    sizes = [len(item["events"]) for item in captured]
    assert sizes[0] == 2
    assert sum(sizes) == 3


async def test_overflow_drops_new_events(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    metrics = Metrics()
    settings = _settings(lifecycle_queue=1)
    emitter = LifecycleEmitter(settings, metrics)
    emitter.emit_start({"session_id": "a"}, cause="spawn")
    emitter.emit_start({"session_id": "b"}, cause="spawn")
    emitter.emit_stop({"session_id": "b", "start_seq": 1}, reason="stop", live_ms=1)
    body = metrics.scrape().decode()
    line = metric_line(body, "apipi_lifecycle_export_total", result="overflow")
    assert line.endswith(" 2.0")
    warnings = [
        record
        for record in caplog.records
        if record.__dict__.get("event") == "lifecycle.export.overflow"
    ]
    assert len(warnings) == 1


def test_first_overflow_warning_is_logged_on_a_fresh_host(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.WARNING, logger="apipi")
    now = [10.0]
    monkeypatch.setattr(
        "apipi.services.lifecycle_export.time",
        SimpleNamespace(monotonic=lambda: now[0]),
    )
    emitter = LifecycleEmitter(_settings(lifecycle_queue=1), Metrics())
    emitter.emit_start({"session_id": "a"}, cause="spawn")
    emitter.emit_start({"session_id": "b"}, cause="spawn")
    now[0] = 20.0
    emitter.emit_start({"session_id": "c"}, cause="spawn")
    warnings = [
        record.__dict__.get("session_id")
        for record in caplog.records
        if record.__dict__.get("event") == "lifecycle.export.overflow"
    ]
    assert warnings == ["b"]


async def test_custom_sinks_isolate_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sink.reset()
    captured = _patch_client(monkeypatch, lambda _request: httpx.Response(204))
    settings = _settings(
        lifecycle_batch_wait=timedelta(0),
        lifecycle_sinks=(
            "tests.support.fake_sink:BoomSink,tests.support.fake_sink:FakeSink"
        ),
    )
    emitter = LifecycleEmitter(settings)
    emitter.start()
    emitter.emit_start({"session_id": "s", "user_id": "ada"}, cause="spawn")
    await emitter.flush(1)
    assert fake_sink.events[-1]["session_id"] == "s"
    assert captured[-1]["events"][0]["session_id"] == "s"


async def test_shutdown_flushes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_client(monkeypatch, lambda _request: httpx.Response(204))
    pool = _pool(_settings(lifecycle_batch_wait=timedelta(0)))
    assert pool.lifecycle is not None
    pool.lifecycle.start()
    sid = await _spawn(pool, monkeypatch, env_type="none")
    await pool.close()
    await pool.lifecycle.close()
    assert captured
    assert captured[-1]["events"][-1]["reason"] == "shutdown"
    assert captured[-1]["events"][-1]["session_id"] == str(sid)


async def test_user_id_modes_on_events(monkeypatch: pytest.MonkeyPatch) -> None:
    hashed = _pool(
        _settings(
            lifecycle_user_id="hash",
            lifecycle_user_id_key="secret",
        )
    )
    await _spawn(hashed, monkeypatch, user_id="ada@ex.com", env_type="none")
    expected = shape_user_id("ada@ex.com", "hash", "secret")
    assert _events(hashed)[0]["user_id"] == expected
    omitted = _pool(_settings(lifecycle_user_id="omit"))
    await _spawn(omitted, monkeypatch, user_id="ada@ex.com", env_type="none")
    assert _events(omitted)[0]["user_id"] is None


def test_backoff_stays_under_cap() -> None:
    for attempt in range(8):
        assert 0 < backoff_seconds(attempt, 0.05) <= 0.05


def test_reconcile_missing_session_and_silent_worker() -> None:
    boot = "boot-a"
    start = {
        "type": "session.live.start",
        "event_id": f"{boot}:1",
        "boot_id": boot,
        "seq": 1,
        "ts": "2026-09-27T18:00:00.000Z",
        "session_id": "s1",
        "worker_id": "w1",
        "instance_id": "i1",
    }
    beat1 = {
        "type": "session.live.heartbeat",
        "event_id": f"{boot}:2",
        "boot_id": boot,
        "seq": 2,
        "ts": "2026-09-27T18:01:00.000Z",
        "worker_id": "w1",
        "instance_id": "i1",
        "interval_s": 60,
        "live": [{"session_id": "s1", "start_seq": 1, "started_at": start["ts"]}],
    }
    beat2 = {
        "type": "session.live.heartbeat",
        "event_id": f"{boot}:3",
        "boot_id": boot,
        "seq": 3,
        "ts": "2026-09-27T18:02:00.000Z",
        "worker_id": "w1",
        "instance_id": "i1",
        "interval_s": 60,
        "live": [],
    }
    missing = reconcile(
        [start, beat1, beat2, beat1],
        now=datetime(2026, 9, 27, 18, 2, 30, tzinfo=UTC),
        grace_s=0,
    )
    assert missing == [
        {
            "boot_id": boot,
            "session_id": "s1",
            "start_seq": 1,
            "started_at": start["ts"],
            "closed_at": "2026-09-27T18:01:00.000Z",
            "reason": "lost",
            "live_ms": None,
        }
    ]
    silent = reconcile(
        [start, beat1],
        now=datetime(2026, 9, 27, 18, 4, 1, tzinfo=UTC),
        grace_s=0,
    )
    assert silent[0]["reason"] == "worker_lost"
    assert silent[0]["closed_at"] == "2026-09-27T18:01:00.000Z"
    other = {
        "type": "session.live.start",
        "event_id": "boot-b:1",
        "boot_id": "boot-b",
        "seq": 1,
        "ts": "2026-09-27T18:03:00.000Z",
        "session_id": "s2",
        "worker_id": "w1",
        "instance_id": "i1",
    }
    replaced = reconcile(
        [start, beat1, other],
        now=datetime(2026, 9, 27, 18, 3, 10, tzinfo=UTC),
        grace_s=30,
    )
    assert replaced[0]["reason"] == "worker_lost"
    assert replaced[0]["closed_at"] == "2026-09-27T18:01:00.000Z"
    assert replaced[0]["session_id"] == "s1"
