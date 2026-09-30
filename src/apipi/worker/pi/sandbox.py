import logging
import re
from typing import Any

from apipi.config import ConfigError, Settings
from apipi.gateway.errors import ApiError

SANDBOX_SIZES = frozenset({"S", "M", "L"})
SANDBOX_SIZE_KEY = "apipi.sandbox_size"
SANDBOX_IMAGE_KEY = "apipi.sandbox_image"
SANDBOX_SIZE_HELP = "sandbox_size must be S, M, or L"
SANDBOX_IMAGE_HELP = "sandbox_image must match ^[a-z0-9][a-z0-9-]{0,31}$"
IMAGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SIZE_RANK = {"S": 0, "M": 1, "L": 2}
_warned_min_vcpus: set[str] = set()
log = logging.getLogger("apipi.worker.pi")


def parse_sandbox_size(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_SIZES:
        raise ApiError(
            "invalid_request",
            SANDBOX_SIZE_HELP,
            code="invalid_request",
        )
    return value


def sandbox_size_of(environment: dict[str, Any] | None) -> str:
    if not environment:
        return "S"
    raw = environment.get("sandbox_size")
    if raw is None:
        return "S"
    parsed = parse_sandbox_size(raw)
    return parsed if parsed is not None else "S"


def image_for_size(size: str) -> str:
    if size == "L":
        return "browser"
    return "default"


def parse_sandbox_image(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or IMAGE_ID.fullmatch(value) is None:
        raise ApiError(
            "invalid_request",
            SANDBOX_IMAGE_HELP,
            code="invalid_request",
        )
    return value


def image_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata or SANDBOX_IMAGE_KEY not in metadata:
        return None
    return parse_sandbox_image(metadata.get(SANDBOX_IMAGE_KEY))


def resolve_sandbox_image(
    *,
    environment_image: str | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    size: str,
    default: str,
    agent_default: str | None = None,
) -> str:
    if environment_image is not None:
        parsed = parse_sandbox_image(environment_image)
        if parsed is not None:
            return parsed
    session_image = image_from_metadata(session_metadata)
    if session_image is not None:
        return session_image
    if agent_default is not None:
        parsed = parse_sandbox_image(agent_default)
        if parsed is not None:
            return parsed
    agent_image = image_from_metadata(agent_metadata)
    if agent_image is not None:
        return agent_image
    if size == "L":
        return "browser"
    parsed = parse_sandbox_image(default)
    return parsed if parsed is not None else "default"


def sandbox_image_of(environment: dict[str, Any] | None) -> str | None:
    if not environment:
        return None
    raw = environment.get("sandbox_image")
    if not isinstance(raw, str):
        return None
    return parse_sandbox_image(raw)


def min_size_for_image(image_id: str) -> str | None:
    from apipi.worker.pi.install import images_root, read_image_env

    try:
        env = read_image_env(images_root() / image_id / "image.env")
    except (OSError, ConfigError):
        env = {}
    raw = env.get("MIN_SIZE")
    if raw in SANDBOX_SIZES:
        return raw
    if image_id in {"browser", "work"}:
        return "M"
    if image_id == "default":
        return "S"
    return None


def require_image_size(image_id: str, size: str) -> None:
    floor = min_size_for_image(image_id)
    if floor is None:
        return
    if _SIZE_RANK[size] < _SIZE_RANK[floor]:
        raise ApiError(
            "invalid_request",
            f"sandbox_image {image_id} needs sandbox_size {floor} or larger",
            code="invalid_request",
        )


def size_for_mem(settings: Settings, mem_mib: int) -> str:
    if mem_mib >= settings.sandbox_l_mem_mib:
        return "L"
    if mem_mib >= settings.sandbox_m_mem_mib:
        return "M"
    return "S"


def size_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    if SANDBOX_SIZE_KEY not in metadata:
        return None
    return parse_sandbox_size(metadata.get(SANDBOX_SIZE_KEY))


def resolve_sandbox_size(
    *,
    environment_size: str | None,
    session_metadata: dict[str, Any] | None,
    agent_metadata: dict[str, Any] | None,
    default: str,
    agent_default: str | None = None,
) -> str:
    if environment_size is not None:
        parsed = parse_sandbox_size(environment_size)
        if parsed is not None:
            return parsed
    session_size = size_from_metadata(session_metadata)
    if session_size is not None:
        return session_size
    if agent_default is not None:
        parsed = parse_sandbox_size(agent_default)
        if parsed is not None:
            return parsed
    agent_size = size_from_metadata(agent_metadata)
    if agent_size is not None:
        return agent_size
    parsed = parse_sandbox_size(default)
    return parsed if parsed is not None else "S"


def mem_mib_for_size(settings: Settings, size: str | None) -> int:
    return settings.sandbox_mem_mib(size if size is not None else "S")


def recommended_min_vcpus(image_id: str, settings: Settings | None = None) -> int:
    if settings is not None:
        from apipi.worker.pi.image_pull import configured_images_dir
        from apipi.worker.pi.images import read_current

        root = configured_images_dir(settings)
        version = read_current(root, image_id)
        if version is not None:
            path = root / image_id / version / "manifest.json"
            if path.is_file():
                import json

                try:
                    data = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    data = None
                if isinstance(data, dict):
                    raw = data.get("min_vcpus")
                    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
                        return raw
    from apipi.worker.pi.install import recipe_env

    raw_text = recipe_env(image_id).get("MIN_VCPUS", "")
    if raw_text.isdigit() and int(raw_text) >= 1:
        return int(raw_text)
    if image_id == "browser":
        return 2
    return 1


def min_vcpus_for_image(image_id: str | None, settings: Settings | None = None) -> int:
    if not image_id:
        return 1
    recommended = recommended_min_vcpus(image_id, settings)
    overrides = settings.sandbox_image_min_vcpus if settings is not None else {}
    if image_id not in overrides:
        return recommended
    floor = overrides[image_id]
    if floor < recommended and image_id not in _warned_min_vcpus:
        _warned_min_vcpus.add(image_id)
        log.warning(
            "image %s: min vcpus %s is below the recommended %s "
            "(APIPI_SANDBOX_IMAGE_MIN_VCPUS)",
            image_id,
            floor,
            recommended,
        )
    return floor


def validate_sandbox_metadata(
    settings: Settings, metadata: dict[str, Any] | None
) -> None:
    if not metadata:
        return
    if SANDBOX_SIZE_KEY not in metadata and SANDBOX_IMAGE_KEY not in metadata:
        return
    size = resolve_sandbox_size(
        environment_size=None,
        session_metadata=None,
        agent_metadata=metadata,
        default=settings.sandbox_default_size,
    )
    image = resolve_sandbox_image(
        environment_image=None,
        session_metadata=None,
        agent_metadata=metadata,
        size=size,
        default=settings.sandbox_default_image,
    )
    require_known_image(settings, image)
    require_image_size(image, size)


def _builtin_images() -> frozenset[str]:
    from apipi.worker.pi.install import recipe_ids

    try:
        found = frozenset(recipe_ids())
    except ConfigError:
        found = frozenset()
    return found | {"default", "browser", "work"}


def require_known_image(settings: Settings, image_id: str) -> None:
    if settings.sandbox_images is not None:
        known = image_id in settings.sandbox_images
    elif image_id in _builtin_images():
        known = True
    else:
        from apipi.worker.pi.image_pull import available_images

        known = any(item.id == image_id for item in available_images(settings))
    if not known:
        raise ApiError(
            "invalid_request",
            f"unknown sandbox_image {image_id}",
            code="invalid_request",
        )


def require_image_rootfs(settings: Settings, image_id: str) -> None:
    if settings.api_only or settings.run_mode != "microvm":
        return
    from apipi.worker.pi.microvm import microvm_images

    try:
        microvm_images(settings, image=image_id)
    except ConfigError as exc:
        raise ApiError(
            "api_error",
            str(exc),
            code="image_unavailable",
            status_code=503,
        ) from exc


def require_size_rootfs(settings: Settings, size: str) -> None:
    require_image_rootfs(settings, image_for_size(size))
