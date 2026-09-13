from typing import Any

events: list[dict[str, Any]] = []


def reset() -> None:
    events.clear()


class FakeSink:
    def emit(self, event: dict[str, Any]) -> None:
        events.append(event)
