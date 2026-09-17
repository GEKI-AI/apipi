import json
import logging

import pytest

from apipi.gateway.logutil import FlushStreamHandler, JsonFormatter, extra_fields


def _record(msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("apipi", logging.INFO, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_baseline() -> None:
    payload = json.loads(JsonFormatter().format(_record()))
    assert payload["level"] == "info"
    assert payload["logger"] == "apipi"
    assert payload["message"] == "hello"
    assert payload["service"] == "apipi"
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_redacts_secrets() -> None:
    payload = json.loads(
        JsonFormatter().format(
            _record(
                authorization="secret",
                api_key="k",
                session_id="s1",
                headers={"Authorization": "Bearer x", "path": "/v1"},
            )
        )
    )
    assert payload["authorization"] == "[redacted]"
    assert payload["api_key"] == "[redacted]"
    assert payload["session_id"] == "s1"
    assert payload["headers"]["Authorization"] == "[redacted]"
    assert payload["headers"]["path"] == "/v1"


def test_json_formatter_omits_none() -> None:
    payload = json.loads(JsonFormatter().format(_record(request_id=None, turn_id="t")))
    assert "request_id" not in payload
    assert payload["turn_id"] == "t"


def test_info_drops_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="apipi")
    log = logging.getLogger("apipi")
    log.debug("hidden")
    log.info("visible")
    assert "visible" in caplog.text
    assert "hidden" not in caplog.text


def test_flush_stream_handler_flushes() -> None:
    stream = _FlushStream()
    handler = FlushStreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("apipi", logging.INFO, __file__, 1, "flushed", (), None)
    handler.emit(record)
    assert stream.flushed >= 1
    assert "flushed" in stream.getvalue()


class _FlushStream:
    def __init__(self) -> None:
        self.buf = ""
        self.flushed = 0

    def write(self, data: str) -> int:
        self.buf += data
        return len(data)

    def flush(self) -> None:
        self.flushed += 1

    def getvalue(self) -> str:
        return self.buf


def test_extra_fields_skip_record_attrs() -> None:
    record = _record(session_id="s")
    fields = extra_fields(record)
    assert fields["session_id"] == "s"
    assert "msg" not in fields
    assert "levelname" not in fields
