from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.sandbox import (
    PLAYWRIGHT_LABEL,
    has_playwright,
    image_for_size,
    merge_playwright,
    playwright_attached,
    resolve_sandbox_size,
    sandbox_size_of,
)


def test_resolve_prefers_environment() -> None:
    size = resolve_sandbox_size(
        environment_size="L",
        session_metadata={"apipi.sandbox_size": "M"},
        agent_metadata={"apipi.sandbox_size": "S"},
        default="S",
    )
    assert size == "L"


def test_resolve_session_metadata_over_agent() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        session_metadata={"apipi.sandbox_size": "M"},
        agent_metadata={"apipi.sandbox_size": "L"},
        default="S",
    )
    assert size == "M"


def test_resolve_agent_metadata_over_default() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        session_metadata={"other": "x"},
        agent_metadata={"apipi.sandbox_size": "L"},
        default="S",
    )
    assert size == "L"


def test_resolve_default() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        session_metadata=None,
        agent_metadata=None,
        default="M",
    )
    assert size == "M"


def test_resolve_rejects_invalid_metadata() -> None:
    try:
        resolve_sandbox_size(
            environment_size=None,
            session_metadata={"apipi.sandbox_size": "XL"},
            agent_metadata=None,
            default="S",
        )
    except ApiError as exc:
        assert exc.status_code == 400
        assert "sandbox_size" in exc.message
    else:
        raise AssertionError("expected ApiError")


def test_image_and_mem() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    assert image_for_size("S") == "default"
    assert image_for_size("M") == "default"
    assert image_for_size("L") == "browser"
    assert settings.sandbox_mem_mib("S") == 512
    assert settings.sandbox_mem_mib("M") == 1024
    assert settings.sandbox_mem_mib("L") == 2048
    assert sandbox_size_of({}) == "S"
    assert sandbox_size_of({"sandbox_size": "L"}) == "L"


def _microvm() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
    )


def test_merge_playwright_on_l_microvm() -> None:
    tools = merge_playwright([], size="L", settings=_microvm())
    assert len(tools) == 1
    assert tools[0]["server_label"] == PLAYWRIGHT_LABEL
    assert tools[0]["transport"]["command"] == "npx"
    assert "@playwright/mcp@latest" in tools[0]["transport"]["args"]
    assert (
        "--executable-path=/usr/bin/chromium-browser" in tools[0]["transport"]["args"]
    )
    assert "--no-sandbox" in tools[0]["transport"]["args"]
    assert "--output-dir=/workspace/outputs" in tools[0]["transport"]["args"]
    assert has_playwright(tools)
    assert playwright_attached(tools)


def test_merge_playwright_skips_none_and_small() -> None:
    none = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    assert merge_playwright([], size="L", settings=none) == []
    assert merge_playwright([], size="S", settings=_microvm()) == []
    assert merge_playwright([], size="M", settings=_microvm()) == []


def test_merge_playwright_respects_auto_off() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="microvm",
        sandbox_auto_playwright=False,
    )
    assert merge_playwright([], size="L", settings=settings) == []


def test_merge_playwright_does_not_duplicate() -> None:
    existing = [
        {
            "type": "mcp",
            "server_label": "playwright",
            "transport": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@1.2.3"],
            },
        }
    ]
    merged = merge_playwright(existing, size="L", settings=_microvm())
    assert merged == existing


def test_merge_playwright_detects_package_without_label() -> None:
    existing = [
        {
            "type": "mcp",
            "server_label": "browser",
            "transport": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest", "--headless"],
            },
        }
    ]
    assert merge_playwright(existing, size="L", settings=_microvm()) == existing
