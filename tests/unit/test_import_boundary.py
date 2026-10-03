import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "apipi"

API_PACKAGES = ("api", "gateway", "services", "store", "workerhub")
SHARED_PACKAGES = ("common", "protocol", "worker")
API_MODULES = tuple(f"apipi.{name}" for name in API_PACKAGES)

DB_AND_HTTP_MODULES = ["sqlalchemy", "asyncpg", "aiosqlite", "fastapi"]
PROTOCOL_ALLOWED_THIRD_PARTY = {"pydantic"}


def _imports(package: str) -> list[tuple[Path, int, str]]:
    found: list[tuple[Path, int, str]] = []
    for path in sorted((SRC / package).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.append((path, node.lineno, node.module))
            elif isinstance(node, ast.Import):
                found.extend((path, node.lineno, alias.name) for alias in node.names)
    return found


def _is(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _where(path: Path, line: int, module: str) -> str:
    return f"{path.relative_to(SRC.parent.parent)}:{line} imports {module}"


def _loaded_after(imports: list[str], banned: list[str]) -> list[str]:
    script = "\n".join(
        [
            "import sys",
            *imports,
            f"banned = {banned!r}",
            "for name in sorted(sys.modules):",
            "    if any(name == b or name.startswith(b + '.') for b in banned):",
            "        print(name)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


def test_run_worker_import_does_not_load_the_api() -> None:
    loaded = _loaded_after(
        ["from apipi.worker.client import run_worker"],
        [*DB_AND_HTTP_MODULES, "starlette", *API_MODULES],
    )
    assert loaded == []


def test_worker_modules_do_not_load_the_api() -> None:
    imports = [
        "import apipi.worker.client",
        "import apipi.worker.execution",
        "import apipi.worker.lifecycle",
        "import apipi.worker.runtime",
        "import apipi.worker.tls",
    ]
    loaded = _loaded_after(imports, [*DB_AND_HTTP_MODULES, *API_MODULES])
    assert loaded == []


@pytest.mark.parametrize("package", API_PACKAGES)
def test_api_packages_do_not_import_the_worker(package: str) -> None:
    bad = [
        _where(path, line, module)
        for path, line, module in _imports(package)
        if _is(module, "apipi.worker")
    ]
    assert bad == []


@pytest.mark.parametrize("package", SHARED_PACKAGES)
def test_shared_and_worker_packages_do_not_import_the_api(package: str) -> None:
    bad = [
        _where(path, line, module)
        for path, line, module in _imports(package)
        if any(_is(module, api) for api in API_MODULES)
    ]
    assert bad == []


def test_common_does_not_import_the_worker() -> None:
    bad = [
        _where(path, line, module)
        for path, line, module in _imports("common")
        if _is(module, "apipi.worker")
    ]
    assert bad == []


def test_protocol_imports_only_pydantic_and_the_standard_library() -> None:
    bad = []
    for path, line, module in _imports("protocol"):
        root = module.split(".")[0]
        if root == "apipi":
            if not _is(module, "apipi.protocol"):
                bad.append(_where(path, line, module))
        elif root not in sys.stdlib_module_names and (
            root not in PROTOCOL_ALLOWED_THIRD_PARTY
        ):
            bad.append(_where(path, line, module))
    assert bad == []


def test_protocol_loads_without_other_apipi_packages() -> None:
    script = "\n".join(
        [
            "import sys",
            "import apipi.protocol",
            "for name in sorted(sys.modules):",
            "    own = name.startswith('apipi.protocol')",
            "    if name.startswith('apipi.') and not own:",
            "        print(name)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == []
