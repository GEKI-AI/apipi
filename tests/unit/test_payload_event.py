import uuid

from apipi.config import Settings
from apipi.payload_export import payload_event, redact_payload
from apipi.store.models import Item


def test_payload_event_includes_turn_items() -> None:
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    other = uuid.uuid4()
    items = [
        Item(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            type="message",
            data={"role": "user", "content": "hello"},
        ),
        Item(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=other,
            type="message",
            data={"role": "user", "content": "skip"},
        ),
        Item(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            type="function_call",
            data={
                "type": "function_call",
                "call_id": "c1",
                "name": "echo",
                "arguments": {"text": "hi"},
            },
        ),
    ]
    event = payload_event(
        tenant_id=tenant_id,
        session_id=session_id,
        turn_id=turn_id,
        request_id="req-1",
        items=items,
    )
    assert event["tenant_id"] == str(tenant_id)
    assert event["request_id"] == "req-1"
    assert event["items"] == [
        {"type": "message", "role": "user", "content": "hello"},
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "echo",
            "arguments": {"text": "hi"},
        },
    ]
    assert "skip" not in str(event)


def test_redact_payload_strips_secrets_and_bearer() -> None:
    body = {
        "items": [
            {
                "type": "message",
                "content": "key=sk-secret Authorization: Bearer abc.def",
            }
        ]
    }
    redacted = redact_payload(body, ("sk-secret",))
    assert redacted["items"][0]["content"] == (
        "key=[redacted] Authorization: Bearer [redacted]"
    )


def test_payload_export_defaults_off() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="host",
    )
    assert settings.payload_export_url is None
