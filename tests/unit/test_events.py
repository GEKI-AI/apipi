import inspect

from apipi.store import events
from apipi.store.events import append_event, list_events


def test_only_append_and_list() -> None:
    public = {
        name
        for name, value in inspect.getmembers(events)
        if inspect.iscoroutinefunction(value)
    }
    assert public == {"append_event", "list_events"}
    assert inspect.iscoroutinefunction(append_event)
    assert inspect.iscoroutinefunction(list_events)
