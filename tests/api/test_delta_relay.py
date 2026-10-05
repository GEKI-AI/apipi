"""Split-mode delta relay: worker socket to SSE over the event bus."""

import asyncio
import json
import uuid
from typing import Any

from tests.support.http import auth, parse_sse, tenant_of

from apipi.api.sessions import _event_stream
from apipi.common.usage import usage_from
from apipi.config import Settings
from apipi.store.engine import Store
from apipi.worker.fake_harness import FakeHarness

CHUNKS = ["hel", "lo ", "wor", "ld"]
FULL_TEXT = "".join(CHUNKS)


class StreamHarness(FakeHarness):
    """A fake streaming model host: spaced fragments, then final text."""

    async def generate(self, text: str, **kwargs: object):  # type: ignore[override]
        del text, kwargs
        for chunk in CHUNKS:
            yield ("agent.session.turn.output_text.delta", {"delta": chunk})
            await asyncio.sleep(0.05)
        yield ("agent.session.turn.output_text.done", {"text": FULL_TEXT})
        yield ("usage", usage_from(self.usage))


def _wire_envelope(raw: str) -> dict[str, Any] | None:
    """Parse one recorded worker-to-API payload as a delta envelope."""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if isinstance(parsed, dict) and parsed.get("type") == "delta.text":
        return parsed
    return None


async def test_split_mode_streams_deltas_before_done(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    from tests.support.split_worker import split_client_for

    token = "delta-split"
    tenant_id = tenant_of(token)
    sent: list[str] = []
    async with split_client_for(
        settings, store, harness=StreamHarness(), token=worker_secret, sent=sent
    ) as (app, client, _worker):
        agent = await client.post(
            "/v1/agents",
            headers=auth(token),
            json={"name": "bot", "model": "test"},
        )
        assert agent.status_code == 200
        created = await client.post(
            "/v1/agents/sessions",
            headers=auth(token),
            json={
                "agent_id": agent.json()["id"],
                "environment": {"type": "none"},
            },
        )
        assert created.status_code == 200
        session_id = uuid.UUID(created.json()["id"])

        agen = _event_stream(
            store,
            app.state.event_hub,
            tenant_id,
            session_id,
            None,
            fallback_poll=0.05,
        )

        async def collect() -> str:
            chunks: list[str] = []
            try:
                async with asyncio.timeout(30):
                    async for chunk in agen:
                        if chunk.startswith(":"):
                            continue
                        chunks.append(chunk)
                        types = [event["type"] for event in parse_sse("".join(chunks))]
                        if "agent.session.turn.completed" in types:
                            return "".join(chunks)
            finally:
                await agen.aclose()
            raise AssertionError("turn never completed on SSE")

        collector = asyncio.create_task(collect())
        posted = await client.post(
            f"/v1/agents/sessions/{session_id}/events",
            headers=auth(token),
            json={"type": "agent.session.input.message", "content": "hello"},
        )
        assert posted.status_code == 200
        raw = await collector
        events = parse_sse(raw)
        kinds = [event["type"] for event in events]
        assert "agent.session.turn.output_text.done" in kinds
        deltas = [
            event
            for event in events
            if event["type"] == "agent.session.turn.output_text.delta"
        ]
        assert len(deltas) >= 3
        first_done = kinds.index("agent.session.turn.output_text.done")
        delta_positions = [
            i
            for i, kind in enumerate(kinds)
            if kind == "agent.session.turn.output_text.delta"
        ]
        assert delta_positions and max(delta_positions) < first_done
        done = next(
            event
            for event in events
            if event["type"] == "agent.session.turn.output_text.done"
        )
        assert done["data"]["text"] == FULL_TEXT
        assert "".join(str(delta["data"]["delta"]) for delta in deltas) == FULL_TEXT
        envelopes = [
            envelope for raw in sent if (envelope := _wire_envelope(raw)) is not None
        ]
        assert len(envelopes) >= 3
        assert all(item["type"] == "delta.text" for item in envelopes)
        assert "".join(str(item["payload"]["text"]) for item in envelopes) == FULL_TEXT

        stored = await client.get(
            f"/v1/agents/sessions/{session_id}/events", headers=auth(token)
        )
        stored_types = [event["type"] for event in stored.json()["data"]]
        assert "agent.session.turn.output_text.delta" not in stored_types
        assert "agent.session.turn.output_text.done" in stored_types
