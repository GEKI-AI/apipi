from apipi.gateway.http_path import request_path, skip_request_path

_SKIP = frozenset({"/health", "/metrics"})


def test_request_path_strips_root_path() -> None:
    assert request_path({"path": "/apipi/health", "root_path": "/apipi"}) == "/health"


def test_request_path_without_root() -> None:
    assert request_path({"path": "/health", "root_path": ""}) == "/health"


def test_skip_matches_raw_path() -> None:
    assert skip_request_path({"path": "/health", "root_path": ""}, _SKIP)


def test_skip_matches_prefixed_path() -> None:
    assert skip_request_path({"path": "/apipi/health", "root_path": "/apipi"}, _SKIP)
    assert skip_request_path({"path": "/apipi/metrics", "root_path": "/apipi"}, _SKIP)


def test_skip_misses_other_paths() -> None:
    assert not skip_request_path(
        {"path": "/apipi/v1/agents", "root_path": "/apipi"}, _SKIP
    )
