import uuid
from pathlib import Path

import pytest

from apipi.worker.outbox import EMERGENCY_BUDGET, Outbox, OutboxFull


def _payload(n: int = 0) -> dict[str, object]:
    return {"n": n, "text": "x" * 10}


def test_append_assigns_monotonic_seq_per_session() -> None:
    outbox = Outbox()
    first_session = uuid.uuid4()
    second_session = uuid.uuid4()
    first = outbox.append(first_session, "event", _payload(1))
    second = outbox.append(first_session, "event", _payload(2))
    other = outbox.append(second_session, "event", _payload(3))
    assert (first["seq"], second["seq"]) == (1, 2)
    assert other["seq"] == 1
    assert first["session_id"] == str(first_session)


def test_ack_prunes_only_acked_prefix() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(5):
        outbox.append(session_id, "event", _payload(n))
    outbox.acked(session_id, 3)
    assert [item["seq"] for item in outbox.pending(session_id)] == [4, 5]
    assert outbox.pending(session_id, after_seq=4)[0]["seq"] == 5


def test_set_base_prunes_and_continues_sequence() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    outbox.set_base(session_id, 2)
    assert [item["seq"] for item in outbox.pending(session_id)] == [3]
    replayed = outbox.append(session_id, "event", _payload(9))
    assert replayed["seq"] == 4
    assert outbox.high_water(session_id) == 4


def test_overflow_raises_outbox_full() -> None:
    outbox = Outbox(max_messages=2)
    session_id = uuid.uuid4()
    outbox.append(session_id, "event", _payload(1))
    outbox.append(session_id, "event", _payload(2))
    with pytest.raises(OutboxFull):
        outbox.append(session_id, "event", _payload(3))


def test_byte_cap_raises_outbox_full() -> None:
    outbox = Outbox(max_bytes=1024)
    session_id = uuid.uuid4()
    with pytest.raises(OutboxFull):
        outbox.append(session_id, "event", {"blob": "y" * 2048})


def test_emergency_budget_covers_the_failure_itself() -> None:
    outbox = Outbox(max_messages=1)
    session_id = uuid.uuid4()
    outbox.append(session_id, "event", _payload(1))
    with pytest.raises(OutboxFull):
        outbox.append(session_id, "event", _payload(2))
    for _ in range(EMERGENCY_BUDGET):
        outbox.append(session_id, "turn.status", {"status": "failed"}, emergency=True)
    with pytest.raises(OutboxFull):
        outbox.append(session_id, "turn.status", {"status": "failed"}, emergency=True)


def test_drop_session_clears_buffer() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    outbox.append(session_id, "event", _payload(1))
    outbox.drop_session(session_id)
    assert outbox.pending(session_id) == []
    assert outbox.pending_sessions() == []
    assert outbox.high_water(session_id) == 0


def test_spool_round_trip(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool")
    outbox.append(session_id, "event", _payload(1))
    outbox.append(session_id, "usage", _payload(2))
    assert (tmp_path / "spool" / f"{session_id}.jsonl").exists()
    outbox.acked(session_id, 1)
    reloaded = Outbox(spool_dir=tmp_path / "spool")
    waters = reloaded.load_spool()
    assert waters[session_id] == 2
    assert [item["seq"] for item in reloaded.pending(session_id)] == [2]


def test_spool_prunes_spool_file_on_ack(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool")
    outbox.append(session_id, "event", _payload(1))
    outbox.acked(session_id, 1)
    assert not (tmp_path / "spool" / f"{session_id}.jsonl").exists()


def test_spool_caps_apply_to_reload(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool")
    for n in range(4):
        outbox.append(session_id, "event", _payload(n))
    assert outbox.describe()["messages"] == 4


async def test_wait_dirty_wakes_on_append() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    assert await outbox.wait_dirty(timeout=0.01) is False
    outbox.append(session_id, "event", _payload(1))
    assert await outbox.wait_dirty(timeout=1) is True
    assert await outbox.wait_dirty(timeout=0.01) is False
    outbox.mark_dirty()
    assert await outbox.wait_dirty(timeout=1) is True


def test_invalid_bounds_rejected() -> None:
    with pytest.raises(ValueError):
        Outbox(max_messages=0)
    with pytest.raises(ValueError):
        Outbox(max_bytes=10)
