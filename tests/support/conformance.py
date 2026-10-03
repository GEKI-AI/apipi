"""Golden transcripts of the worker protocol: loading, matching, rendering.

The fixture format is described in `docs/worker-protocol.md`. A transcript
is a JSON Lines file. The first line is the header, every other line is
one step. A player plays one peer of the socket and checks the other.
"""

import json
import re
import uuid
from pathlib import Path
from typing import Any

from apipi.common.timefmt import utc_ts

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "worker-protocol"

SKIPPABLE = frozenset({"delta.text", "delta.reasoning", "heartbeat", "sandbox.seen"})
PLACEHOLDER = re.compile(r"^<(\w+)(?::(\w+))?>$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


class Fixture:
    def __init__(self, name: str, header: dict[str, Any], steps: list[dict[str, Any]]):
        self.name = name
        self.header = header
        self.steps = steps

    @property
    def modes(self) -> list[str]:
        return list(self.header.get("modes", ["api", "worker"]))


def load(name: str) -> Fixture:
    lines = (FIXTURE_DIR / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines if line.strip()]
    return Fixture(name, rows[0], rows[1:])


def names(mode: str | None = None) -> list[str]:
    found = sorted(path.stem for path in FIXTURE_DIR.glob("*.jsonl"))
    if mode is None:
        return found
    return [name for name in found if mode in load(name).modes]


def is_frame(step: dict[str, Any]) -> bool:
    return "frame" in step


def frame_type(frame: dict[str, Any]) -> str | None:
    kind = frame.get("type")
    return kind if isinstance(kind, str) else None


def _same_number(left: Any, right: Any) -> bool:
    return (
        isinstance(right, int | float) and not isinstance(right, bool) and left == right
    )


def match(
    expected: Any, actual: Any, binds: dict[str, Any], path: str = "$"
) -> list[str]:
    """Compare an expected value with an actual one.

    Objects match when every expected key matches; extra keys of the
    actual object are ignored. Lists match element by element. Names in
    placeholders are bound on first sight and must repeat exactly.
    """
    if isinstance(expected, str):
        placeholder = PLACEHOLDER.match(expected)
        if placeholder is not None and placeholder.group(1) in PLACEHOLDERS:
            return _match_placeholder(
                placeholder.group(1), placeholder.group(2), actual, binds, path
            )
        return [] if expected == actual else [f"{path}: {actual!r} != {expected!r}"]
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected an object, got {actual!r}"]
        errors: list[str] = []
        for key, value in expected.items():
            key = _key(key, binds)
            if value == "<absent>":
                if key in actual:
                    errors.append(f"{path}.{key}: must be absent")
            elif key not in actual:
                errors.append(f"{path}.{key}: missing")
            else:
                errors.extend(match(value, actual[key], binds, f"{path}.{key}"))
        return errors
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return [f"{path}: expected a list of {len(expected)}, got {actual!r}"]
        found: list[str] = []
        for index, value in enumerate(expected):
            found.extend(match(value, actual[index], binds, f"{path}[{index}]"))
        return found
    if isinstance(expected, bool) or expected is None:
        return [] if expected is actual else [f"{path}: {actual!r} != {expected!r}"]
    if isinstance(expected, int | float):
        return (
            []
            if _same_number(expected, actual)
            else [f"{path}: {actual!r} != {expected!r}"]
        )
    return [f"{path}: unsupported fixture value {expected!r}"]


def _key(key: str, binds: dict[str, Any]) -> str:
    placeholder = PLACEHOLDER.match(key)
    if placeholder is not None and placeholder.group(1) == "var":
        return str(binds[str(placeholder.group(2))])
    return key


def _bind(name: str | None, value: Any, binds: dict[str, Any], path: str) -> list[str]:
    if name is None:
        return []
    if name in binds and binds[name] != value:
        return [f"{path}: {value!r} != {binds[name]!r} (bound as {name})"]
    binds[name] = value
    return []


def _match_placeholder(
    kind: str, name: str | None, actual: Any, binds: dict[str, Any], path: str
) -> list[str]:
    if kind == "any":
        return []
    if kind == "var":
        if name not in binds:
            return [f"{path}: variable {name} is not bound"]
        return [] if binds[name] == actual else [f"{path}: {actual!r} != <{name}>"]
    if kind == "store_check":
        if not isinstance(actual, dict):
            return [f"{path}: expected a store check"]
        marker, nonce = actual.get("marker"), actual.get("nonce")
        if not isinstance(marker, str) or not isinstance(nonce, str):
            return [f"{path}: store check needs marker and nonce"]
        binds["marker"], binds["nonce"] = marker, nonce
        return []
    checks = {
        "string": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool),
        "bool": lambda v: isinstance(v, bool),
        "uuid": lambda v: isinstance(v, str) and UUID_RE.match(v) is not None,
        "ts": lambda v: isinstance(v, str) and TS_RE.match(v) is not None,
    }
    if not checks[kind](actual):
        return [f"{path}: {actual!r} is not a {kind}"]
    return _bind(name, actual, binds, path)


PLACEHOLDERS = frozenset(
    {"any", "var", "store_check", "string", "int", "number", "bool", "uuid", "ts"}
)


def render(
    value: Any, binds: dict[str, Any], *, store_check: Any = None, path: str = "$"
) -> Any:
    """Turn a fixture value into a frame to send; placeholders get values."""
    if isinstance(value, dict):
        return {
            _key(key, binds): render(
                item, binds, store_check=store_check, path=f"{path}.{key}"
            )
            for key, item in value.items()
            if item != "<absent>"
        }
    if isinstance(value, list):
        return [render(item, binds, store_check=store_check) for item in value]
    if not isinstance(value, str):
        return value
    placeholder = PLACEHOLDER.match(value)
    if placeholder is None or placeholder.group(1) not in PLACEHOLDERS:
        return value
    kind, name = placeholder.group(1), placeholder.group(2)
    if kind == "var":
        if name not in binds:
            raise KeyError(f"{path}: variable {name} is not bound")
        return binds[name]
    if kind == "store_check":
        if store_check is None:
            raise KeyError(f"{path}: no store check available")
        marker, nonce = store_check()
        binds["marker"], binds["nonce"] = marker, nonce
        return {"marker": marker, "nonce": nonce}
    if name is not None and name in binds:
        return binds[name]
    made: Any
    if kind == "uuid":
        made = str(uuid.uuid4())
    elif kind == "string":
        made = name or "string"
    elif kind == "int":
        made = 1
    elif kind == "number":
        made = 1.0
    elif kind == "bool":
        made = True
    elif kind == "ts":
        made = utc_ts()
    else:
        raise KeyError(f"{path}: cannot send {value}")
    if name is not None:
        binds[name] = made
    return made


def ack_target(frame: dict[str, Any]) -> bool:
    return frame_type(frame) == "ack"


def matches_step(
    step: dict[str, Any], actual: dict[str, Any], binds: dict[str, Any]
) -> list[str]:
    """Match one received frame with one frame step, on a copy of the binds."""
    expected = step["frame"]
    trial = dict(binds)
    if ack_target(expected) and ack_target(actual):
        bound = dict(trial)
        errors = match(
            {k: v for k, v in expected.items() if k != "last_seq"}, actual, bound
        )
        if not errors and not actual["last_seq"] >= expected["last_seq"]:
            errors = [f"$.last_seq: {actual['last_seq']} < {expected['last_seq']}"]
        if not errors:
            binds.update(bound)
        return errors
    errors = match(expected, actual, trial)
    if not errors:
        binds.update(trial)
    return errors


def skippable(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    """May an unlisted `actual` frame be passed over while expecting `expected`?"""
    kind = frame_type(actual)
    if kind in SKIPPABLE and kind != frame_type(expected):
        return True
    return ack_target(expected) and ack_target(actual)


async def expect(
    step: dict[str, Any],
    next_frame: Any,
    binds: dict[str, Any],
    label: str,
) -> None:
    """Wait for the frame of `step`; unlisted periodic frames are passed over."""
    while True:
        actual = await next_frame()
        errors = matches_step(step, actual, binds)
        if not errors:
            return
        if skippable(step["frame"], actual):
            continue
        raise AssertionError(
            f"{label}: expected {json.dumps(step['frame'])}\n"
            f"got {json.dumps(actual)}\n" + "\n".join(errors)
        )
