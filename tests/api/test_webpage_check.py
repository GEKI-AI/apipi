import importlib.util
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

from apipi.config import Settings
from apipi.gateway import Gateway
from apipi.services.runtime import FakeHarness
from apipi.store.engine import Store

_APP = Path(__file__).resolve().parents[2] / "examples" / "webpage-check" / "app.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("webpage_check_app", _APP)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_flatten_bash_and_summary() -> None:
    flatten_event = _load().flatten_event
    seen = [False]
    assert (
        flatten_event(
            {
                "type": "agent.session.turn.item.added",
                "data": {"item_type": "command_execution", "name": "bash"},
            },
            seen,
        )
        == "using bash…\n"
    )
    assert (
        flatten_event(
            {
                "type": "agent.session.turn.output_text.delta",
                "data": {"delta": "Hello"},
            },
            seen,
        )
        == "Hello"
    )
    assert (
        flatten_event(
            {
                "type": "agent.session.turn.output_text.done",
                "data": {"text": "Hello"},
            },
            seen,
        )
        is None
    )


def test_flatten_done_without_delta() -> None:
    flatten_event = _load().flatten_event
    seen = [False]
    assert (
        flatten_event(
            {
                "type": "agent.session.turn.output_text.done",
                "data": {"text": "Summary"},
            },
            seen,
        )
        == "Summary\n"
    )


async def test_webpage_check_streams_text(settings: Settings, store: Store) -> None:
    mod = _load()
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    app = mod.build_app(gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        response = await client.post(
            "/examples/webpage-check",
            json={"url": "https://example.com"},
        )
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "https://example.com" in response.text


async def test_webpage_check_rejects_bad_url(settings: Settings, store: Store) -> None:
    mod = _load()
    gateway = Gateway.create(settings, store=store, harness=FakeHarness())
    app = mod.build_app(gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/examples/webpage-check",
            json={"url": "file:///etc/passwd"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
