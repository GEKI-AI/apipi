import pytest

from apipi.config import Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.sandbox import (
    image_for_size,
    min_vcpus_for_image,
    require_image_size,
    resolve_sandbox_image,
    resolve_sandbox_size,
    sandbox_size_of,
    validate_sandbox_metadata,
)


def test_resolve_prefers_environment() -> None:
    size = resolve_sandbox_size(
        environment_size="L",
        default="S",
    )
    assert size == "L"


def test_resolve_agent_default_over_default() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        default="S",
        agent_default="L",
    )
    assert size == "L"


def test_resolve_default() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        default="M",
    )
    assert size == "M"


def test_resolve_rejects_invalid_environment() -> None:
    try:
        resolve_sandbox_size(
            environment_size="XL",
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


def test_work_image_rejects_size_s() -> None:
    with pytest.raises(
        ApiError, match="sandbox_image work needs sandbox_size M"
    ) as exc:
        require_image_size("work", "S")
    assert exc.value.status_code == 400
    require_image_size("work", "M")
    require_image_size("work", "L")


def _none_settings(sandbox_images: list[str] | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_images=sandbox_images,
    )


def test_validate_sandbox_metadata_ignores_bad_size() -> None:
    validate_sandbox_metadata(_none_settings(), {"apipi.sandbox_size": "xl"})


def test_validate_sandbox_metadata_rejects_unknown_image() -> None:
    settings = _none_settings(sandbox_images=["default", "browser"])
    with pytest.raises(ApiError, match="unknown sandbox_image") as exc:
        validate_sandbox_metadata(settings, {"apipi.sandbox_image": "notreal"})
    assert exc.value.status_code == 400


def test_validate_sandbox_metadata_accepts_browser() -> None:
    validate_sandbox_metadata(
        _none_settings(),
        {"apipi.sandbox_image": "browser"},
    )


def test_validate_sandbox_metadata_ignores_size_key() -> None:
    validate_sandbox_metadata(_none_settings(), {"apipi.sandbox_size": "L"})


def test_validate_sandbox_metadata_image_only_uses_default_size() -> None:
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
        {"apipi.sandbox_image": "browser"},
    )


def test_browser_image_has_two_vcpu_floor() -> None:
    assert min_vcpus_for_image("browser") == 2
    assert min_vcpus_for_image("default") == 1
    assert min_vcpus_for_image("work") == 1


def test_image_min_vcpus_override_replaces_floor() -> None:
    from apipi.worker.pi.microvm import guest_vcpus

    lowered = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_image_min_vcpus={"browser": 1},
    )
    assert min_vcpus_for_image("browser", lowered) == 1
    assert guest_vcpus(lowered, 1024, image="browser") == 1
    assert guest_vcpus(lowered, 2048, image="browser") == 2
    raised = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_image_min_vcpus={"work": 4},
    )
    assert min_vcpus_for_image("work", raised) == 4
    assert guest_vcpus(raised, 1024, image="work") == 4
    unset = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    assert min_vcpus_for_image("browser", unset) == 2
    assert guest_vcpus(unset, 1024, image="browser") == 2
    other = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_image_min_vcpus={"nope": 8},
    )
    assert min_vcpus_for_image("browser", other) == 2


def test_image_min_vcpus_warns_once_when_below_recommended(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from apipi.worker.pi import sandbox as sandbox_mod

    sandbox_mod._warned_min_vcpus.clear()
    caplog.set_level("WARNING", logger="apipi.worker.pi")
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sandbox_image_min_vcpus={"browser": 1},
    )
    assert min_vcpus_for_image("browser", settings) == 1
    assert min_vcpus_for_image("browser", settings) == 1
    notes = [
        record.message
        for record in caplog.records
        if "min vcpus 1 is below the recommended 2" in record.message
    ]
    assert len(notes) == 1
