import logging
from typing import Any


def field(record: logging.LogRecord, name: str) -> Any:
    return getattr(record, name, None)
