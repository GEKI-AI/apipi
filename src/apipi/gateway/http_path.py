from starlette.types import Scope


def request_path(scope: Scope) -> str:
    raw = scope.get("path")
    path = raw if isinstance(raw, str) else ""
    root_raw = scope.get("root_path")
    root = root_raw if isinstance(root_raw, str) else ""
    if root and path.startswith(root):
        rest = path[len(root) :]
        if not rest:
            return "/"
        return rest if rest.startswith("/") else f"/{rest}"
    return path


def skip_request_path(scope: Scope, skip: frozenset[str]) -> bool:
    raw = scope.get("path")
    path = raw if isinstance(raw, str) else ""
    if path in skip:
        return True
    return request_path(scope) in skip
