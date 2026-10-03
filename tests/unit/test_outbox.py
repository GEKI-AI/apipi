import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from apipi.protocol import MAX_MESSAGE_BYTES
from apipi.worker.outbox import (
    EMERGENCY_BUDGET,
    FLOOR_LIMIT,
    EnvelopeTooLarge,
    Outbox,
    OutboxFull,
)


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
    outbox = Outbox(max_messages=2, session_share=1.0)
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


async def test_spool_round_trip(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool", compact_min=1)
    outbox.append(session_id, "event", _payload(1))
    outbox.append(session_id, "usage", _payload(2))
    assert (tmp_path / "spool" / f"{session_id}.jsonl").exists()
    outbox.acked(session_id, 1)
    await outbox.maintain_spool()
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


def test_session_share_keeps_one_session_from_filling_the_outbox() -> None:
    outbox = Outbox(max_messages=10, session_share=0.5)
    noisy = uuid.uuid4()
    quiet = uuid.uuid4()
    for n in range(5):
        outbox.append(noisy, "event", _payload(n))
    with pytest.raises(OutboxFull):
        outbox.append(noisy, "event", _payload(5))
    for n in range(5):
        outbox.append(quiet, "event", _payload(n))
    assert outbox.describe()["messages"] == 10


def test_emergency_budget_still_works_past_the_session_share() -> None:
    outbox = Outbox(max_messages=10, session_share=0.5)
    session_id = uuid.uuid4()
    for n in range(5):
        outbox.append(session_id, "event", _payload(n))
    outbox.append(session_id, "turn.status", {"status": "failed"}, emergency=True)


def test_oversize_envelope_is_refused_not_buffered() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    with pytest.raises(EnvelopeTooLarge) as caught:
        outbox.append(session_id, "event", {"blob": "y" * (MAX_MESSAGE_BYTES + 1)})
    assert caught.value.code == "worker_message_too_large"
    assert isinstance(caught.value, OutboxFull)
    assert outbox.pending(session_id) == []
    assert outbox.high_water(session_id) == 0
    assert outbox.describe()["messages"] == 0


def _send_all(outbox: Outbox, session_id: uuid.UUID) -> list[int]:
    sent = []
    while (envelope := outbox.next_unsent(session_id)) is not None:
        outbox.mark_sent(session_id, envelope)
        sent.append(envelope["seq"])
    return sent


def test_pump_sends_each_envelope_once_per_connection() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    assert _send_all(outbox, session_id) == [1, 2, 3]
    outbox.append(session_id, "event", _payload(3))
    assert _send_all(outbox, session_id) == [4]
    assert outbox.unsent_sessions() == []


def test_reconnect_resends_everything_unacked_and_counts_it() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    _send_all(outbox, session_id)
    outbox.append(session_id, "event", _payload(3))
    outbox.acked(session_id, 1)
    assert outbox.begin_connection() == 2
    assert _send_all(outbox, session_id) == [2, 3, 4]
    assert outbox.begin_connection() == 3


def test_first_connection_is_not_a_replay() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    assert outbox.begin_connection() == 0
    assert _send_all(outbox, session_id) == [1, 2, 3]


def test_ack_drops_unsent_envelopes_too() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    outbox.acked(session_id, 2)
    assert _send_all(outbox, session_id) == [3]


def test_release_forgets_an_empty_buffer_and_keeps_the_numbering() -> None:
    outbox = Outbox()
    session_id = uuid.uuid4()
    outbox.append(session_id, "event", _payload(1))
    outbox.release(session_id)
    assert outbox.pending_sessions() == [session_id]
    outbox.acked(session_id, 1)
    outbox.release(session_id)
    assert outbox.describe()["sessions"] == 0
    assert outbox.high_water(session_id) == 1
    assert outbox.acked_seq(session_id) == 1
    assert outbox.append(session_id, "event", _payload(2))["seq"] == 2


def test_release_is_bounded() -> None:
    outbox = Outbox()
    for _ in range(FLOOR_LIMIT + 5):
        outbox.release(uuid.uuid4())
    assert len(outbox._floors) <= FLOOR_LIMIT


async def test_spool_is_append_only_until_compaction(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    path = tmp_path / "spool" / f"{session_id}.jsonl"
    outbox = Outbox(spool_dir=tmp_path / "spool", compact_min=4)
    for n in range(6):
        outbox.append(session_id, "event", _payload(n))
    before = path.read_text()
    outbox.acked(session_id, 2)
    assert path.read_text() == before
    await outbox.maintain_spool()
    assert path.read_text() == before
    outbox.acked(session_id, 5)
    outbox.append(session_id, "event", _payload(6))
    await outbox.maintain_spool()
    lines = path.read_text().splitlines()
    assert [json.loads(line)["seq"] for line in lines] == [6, 7]
    reloaded = Outbox(spool_dir=tmp_path / "spool")
    reloaded.load_spool()
    assert [item["seq"] for item in reloaded.pending(session_id)] == [6, 7]


async def test_compaction_keeps_envelopes_appended_while_it_runs(
    tmp_path: Path,
) -> None:
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool", compact_min=1)
    for n in range(3):
        outbox.append(session_id, "event", _payload(n))
    outbox.acked(session_id, 2)
    task = asyncio.create_task(outbox.maintain_spool())
    await asyncio.sleep(0)
    outbox.append(session_id, "event", _payload(3))
    await task
    reloaded = Outbox(spool_dir=tmp_path / "spool")
    reloaded.load_spool()
    assert [item["seq"] for item in reloaded.pending(session_id)] == [3, 4]


async def test_spool_fsync_runs_on_the_maintenance_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr("apipi.worker.outbox.os.fsync", counting)
    session_id = uuid.uuid4()
    outbox = Outbox(spool_dir=tmp_path / "spool")
    outbox.append(session_id, "event", _payload(1))
    outbox.append(session_id, "event", _payload(2))
    assert calls == []
    await outbox.maintain_spool()
    assert len(calls) == 2
    await outbox.maintain_spool()
    assert len(calls) == 2


def test_worker_killed_mid_turn_replays_from_the_spool(tmp_path: Path) -> None:
    session_id = uuid.uuid4()
    first = Outbox(spool_dir=tmp_path / "spool")
    first.append(session_id, "turn.status", {"status": "started"})
    first.append(session_id, "item.added", _payload(1))
    first.append(session_id, "item.done", _payload(2))
    first.acked(session_id, 1)
    path = tmp_path / "spool" / f"{session_id}.jsonl"
    with path.open("a") as handle:
        handle.write('{"v":2,"seq":4,"ty')
    del first

    restarted = Outbox(spool_dir=tmp_path / "spool")
    waters = restarted.load_spool()
    assert waters == {session_id: 3}
    assert restarted.spool_skipped == 1
    assert restarted.begin_connection() == 3
    assert _send_all(restarted, session_id) == [1, 2, 3]
    restarted.set_base(session_id, 1)
    restarted.begin_connection()
    assert _send_all(restarted, session_id) == [2, 3]
    assert restarted.append(session_id, "event", _payload(3))["seq"] == 4
    restarted.acked(session_id, 4)
    assert not path.exists()
