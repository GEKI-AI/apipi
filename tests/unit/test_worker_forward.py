import uuid
from typing import Any

from prometheus_client import generate_latest

from apipi.common.event_bus import InMemoryEventBus, forward_message
from apipi.common.metrics import Metrics
from apipi.protocol import TurnStartCommandPayload
from apipi.services.event_bus import instance_channel
from apipi.workerhub.fleet import Candidate, choose, has_image
from apipi.workerhub.forward import failure_reason, forward_body


def _candidate(**changes: Any) -> Candidate:
    values: dict[str, Any] = {
        "worker_id": uuid.uuid4(),
        "accepts": frozenset({"none", "microvm"}),
        "images": frozenset({"default"}),
        "arch": "x86_64",
        "draining": False,
        "capacity": 2,
        "memory_mb": 2048,
        "leases": 0,
        "used_mem": 0,
        "instance": "node-b",
    }
    values.update(changes)
    return Candidate(**values)


def test_instance_channel_fits_the_identifier_limit() -> None:
    name = instance_channel("x" * 200)
    assert len(name.encode()) <= 63
    assert name != instance_channel("y" * 200)


def test_forward_message_is_tiny() -> None:
    message = forward_message(uuid.uuid4(), origin="node-a-1234")
    assert len(str(message)) < 200


def test_forward_body_drops_the_context_and_placement_fields() -> None:
    payload = TurnStartCommandPayload.model_validate(
        {
            "tenant_id": uuid.uuid4(),
            "text": "hi",
            "context": {"model": {"api_key": "secret"}},
            "run_mode": "none",
            "last_seq": 4,
        }
    )
    body = forward_body("turn.start", payload)
    assert body["has_context"] is True
    assert "secret" not in str(body)
    assert set(body["payload"]) == {"tenant_id", "text"}


def test_choose_skips_draining_full_and_missing_image() -> None:
    draining = _candidate(draining=True)
    full = _candidate(leases=2)
    no_image = _candidate(images=frozenset())
    free = _candidate()
    chosen = choose(
        [draining, full, no_image, free],
        kind="microvm",
        session_mem=512,
        image="default",
    )
    assert chosen is free
    assert has_image([no_image], "microvm", "default") is False


def test_choose_prefers_the_most_free_memory() -> None:
    busy = _candidate(used_mem=1024, leases=1)
    idle = _candidate()
    assert choose([busy, idle], kind="none", session_mem=512) is idle


def test_failure_reasons() -> None:
    assert failure_reason("worker_unreachable") == "not_connected"
    assert failure_reason("forward_timeout") == "timeout"
    assert failure_reason("capacity") == "rejected"
    assert failure_reason(None) == "error"


async def test_in_memory_bus_delivers_instance_messages() -> None:
    bus = InMemoryEventBus()
    got: list[dict[str, Any]] = []
    await bus.listen_instance("a", got.append)
    await bus.send_instance("a", {"kind": "forward"})
    await bus.send_instance("b", {"kind": "forward"})
    await bus.unlisten_instance("a")
    await bus.send_instance("a", {"kind": "forward"})
    assert got == [{"kind": "forward"}]
    assert bus.forwards is False


def test_forward_metrics_are_exposed() -> None:
    metrics = Metrics()
    metrics.observe_worker_forward("turn.cancel", "ok", 0.01)
    metrics.observe_worker_forward_failure("replica_stale")
    text = generate_latest(metrics.registry).decode()
    assert 'apipi_worker_forwards_total{op="turn.cancel",result="ok"} 1.0' in text
    assert 'apipi_worker_forward_failures_total{reason="replica_stale"} 1.0' in text
    assert "apipi_worker_forward_seconds_bucket" in text
