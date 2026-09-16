from apipi.config import Settings
from apipi.errors import ApiError
from apipi.sandbox import (
    image_for_size,
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
