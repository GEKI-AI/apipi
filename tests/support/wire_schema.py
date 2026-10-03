"""Check the frames the real API and worker send against the committed schema."""

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from apipi.protocol.schema import API_TO_WORKER, WORKER_TO_API, schema_name_for

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "docs" / "worker-protocol" / "schema"

captured: list[tuple[str, str]] | None = None


def start_capture() -> None:
    global captured
    captured = []


def stop_capture() -> list[tuple[str, str]]:
    global captured
    frames, captured = captured or [], None
    return frames


def record(direction: str, text: str) -> None:
    if captured is not None:
        captured.append((direction, text))


@cache
def validator(name: str) -> Any:
    schema = json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def frame_errors(direction: str, frame: Any) -> list[str]:
    if not isinstance(frame, dict):
        return [f"{direction}: not an object"]
    name = schema_name_for(direction, frame)
    if name is None:
        return [f"{direction}: no schema for type {frame.get('type')!r}"]
    errors = []
    for error in validator(name).iter_errors(frame):
        where = "/".join(map(str, error.absolute_path))
        errors.append(f"{direction} {name}: {where}: {error.message}")
    return errors


def capture_errors(frames: list[tuple[str, str]]) -> list[str]:
    errors: list[str] = []
    for direction, text in frames:
        try:
            frame = json.loads(text)
        except ValueError:
            errors.append(f"{direction}: not JSON: {text[:80]}")
            continue
        errors.extend(frame_errors(direction, frame))
    return errors


__all__ = ["API_TO_WORKER", "WORKER_TO_API"]
