import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.sandbox import (
    PLAYWRIGHT_LABEL,
    has_playwright,
    image_for_size,
    merge_playwright,
    playwright_attached,
    require_image_size,
    resolve_sandbox_image,
    resolve_sandbox_size,
    sandbox_size_of,
    validate_sandbox_metadata,
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


def test_resolve_sandbox_image_order() -> None:
    assert (
        resolve_sandbox_image(
            environment_image="default",
            session_metadata={"apipi.sandbox_image": "browser"},
            agent_metadata={"apipi.sandbox_image": "browser"},
            size="S",
            default="browser",
        )
        == "default"
    )
    assert (
        resolve_sandbox_image(
            environment_image=None,
            session_metadata={"apipi.sandbox_image": "browser"},
            agent_metadata={"apipi.sandbox_image": "default"},
            size="S",
            default="default",
        )
        == "browser"
    )
    assert (
        resolve_sandbox_image(
            environment_image=None,
            session_metadata=None,
            agent_metadata={"apipi.sandbox_image": "browser"},
            size="S",
            default="default",
        )
        == "browser"
    )
    assert (
        resolve_sandbox_image(
            environment_image=None,
            session_metadata=None,
            agent_metadata=None,
            size="L",
            default="default",
        )
        == "browser"
    )
    assert (
        resolve_sandbox_image(
            environment_image=None,
            session_metadata=None,
            agent_metadata=None,
            size="S",
            default="default",
        )
        == "default"
    )


def test_browser_image_rejects_size_s() -> None:
    with pytest.raises(ApiError, match="needs sandbox_size M"):
        require_image_size("browser", "S")
    require_image_size("default", "L")


def _none_settings(sandbox_images: list[str] | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_images=sandbox_images,
    )


def test_validate_sandbox_metadata_rejects_bad_size() -> None:
    with pytest.raises(ApiError) as exc:
        validate_sandbox_metadata(_none_settings(), {"apipi.sandbox_size": "xl"})
    assert exc.value.status_code == 400
    assert exc.value.code == "invalid_request"


def test_validate_sandbox_metadata_rejects_unknown_image() -> None:
    settings = _none_settings(sandbox_images=["default", "browser"])
    with pytest.raises(ApiError, match="unknown sandbox_image") as exc:
        validate_sandbox_metadata(settings, {"apipi.sandbox_image": "notreal"})
    assert exc.value.status_code == 400


def test_validate_sandbox_metadata_rejects_browser_on_s() -> None:
    with pytest.raises(ApiError, match="needs sandbox_size M"):
        validate_sandbox_metadata(
            _none_settings(),
            {"apipi.sandbox_image": "browser", "apipi.sandbox_size": "S"},
        )


def test_validate_sandbox_metadata_accepts_browser_on_m() -> None:
    validate_sandbox_metadata(
        _none_settings(),
        {"apipi.sandbox_image": "browser", "apipi.sandbox_size": "M"},
    )


def test_validate_sandbox_metadata_size_only_l_selects_browser() -> None:
    validate_sandbox_metadata(_none_settings(), {"apipi.sandbox_size": "L"})


def test_validate_sandbox_metadata_image_only_uses_default_size() -> None:
    with pytest.raises(ApiError, match="needs sandbox_size M"):
        validate_sandbox_metadata(_none_settings(), {"apipi.sandbox_image": "browser"})


def test_validate_sandbox_metadata_ignores_other_keys() -> None:
    validate_sandbox_metadata(_none_settings(), {"keep": "me"})
    validate_sandbox_metadata(_none_settings(), None)


def test_validate_sandbox_metadata_skips_worker_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("worker availability checked")

    monkeypatch.setattr(
        "apipi.worker.pi.sandbox.require_image_rootfs",
        boom,
    )
    validate_sandbox_metadata(
        _microvm(),
        {"apipi.sandbox_image": "browser", "apipi.sandbox_size": "M"},
    )


def test_merge_playwright_follows_image_not_size() -> None:
    assert merge_playwright([], size="M", image="browser", settings=_microvm())
    assert merge_playwright([], size="L", image="default", settings=_microvm()) == []


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
