from apipi.env.hub import VERBS
from apipi.worker import COMMAND_OPS, WORKER_IN


def test_worker_protocol_is_not_self_hosted() -> None:
    assert COMMAND_OPS.isdisjoint(VERBS)
    assert "hello" not in WORKER_IN
    assert "/v1/environments/" not in "/internal/worker"
    assert {"turn.start", "turn.cancel", "turn.continue"} == COMMAND_OPS
    assert "register" in WORKER_IN
    assert "heartbeat" in WORKER_IN
    assert "lease.ack" in WORKER_IN
    assert "event" in WORKER_IN
