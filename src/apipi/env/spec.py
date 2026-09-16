from typing import Any, Literal, Self

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from apipi.env.setup import (
    SetupError,
    inline_files_from,
    packages_from,
    session_env_from,
    session_network_from,
    setup_commands_from,
)
from apipi.errors import ApiError, not_implemented
from apipi.schemas import StrictModel

_UNIMPLEMENTED = (
    "environment_template_id",
    "skills",
    "plugins",
)


class PackagesSpec(StrictModel):
    python: list[str] | None = None
    system: list[str] | None = None
    npm: list[str] | None = None


class InlineFileSpec(StrictModel):
    type: Literal["inline"]
    path: str
    data: str


class NetworkSpec(StrictModel):
    access: Literal["enabled", "disabled", "restricted"]
    allowed_domains: list[str] | None = None


class SetupCommandSpec(StrictModel):
    command: str
    cwd: str | None = None

    @model_validator(mode="after")
    def command_present(self) -> Self:
        if not self.command.strip():
            raise ValueError("setup_commands need a command")
        return self


class EnvironmentSpec(StrictModel):
    type: str
    capability_directories: list[str] | None = None
    packages: PackagesSpec | None = None
    setup_commands: list[SetupCommandSpec] | None = None
    sandbox_size: Literal["S", "M", "L"] | None = None
    env: dict[str, str] | None = None
    files: list[InlineFileSpec] | None = None
    network: NetworkSpec | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_unimplemented(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for field in _UNIMPLEMENTED:
                if field in data:
                    raise PydanticCustomError(
                        "not_implemented",
                        "{field} is not implemented",
                        {"field": field},
                    )
            raw_files = data.get("files")
            if isinstance(raw_files, list):
                for item in raw_files:
                    if isinstance(item, dict) and item.get("type") != "inline":
                        raise PydanticCustomError(
                            "not_implemented",
                            "{field} is not implemented",
                            {"field": "files"},
                        )
        return data


def environment_payload(spec: EnvironmentSpec | None) -> dict[str, Any]:
    env_type = spec.type if spec is not None else "openai_hosted"
    if env_type == "hosted":
        env_type = "openai_hosted"
    if env_type not in {"none", "openai_hosted", "self_hosted"}:
        not_implemented(env_type)
    payload: dict[str, Any] = {"type": env_type}
    if spec is None:
        return payload
    hosted_only = (
        spec.packages is not None
        or spec.setup_commands is not None
        or spec.env is not None
        or spec.files is not None
    )
    if hosted_only and env_type != "openai_hosted":
        raise ApiError(
            "invalid_request",
            "packages, setup_commands, env, and files need openai_hosted",
            code="invalid_request",
        )
    if spec.capability_directories is not None:
        payload["capability_directories"] = spec.capability_directories
    if spec.sandbox_size is not None:
        payload["sandbox_size"] = spec.sandbox_size
    if spec.packages is not None:
        packages = spec.packages.model_dump(exclude_none=True)
        if packages:
            payload["packages"] = packages
    if spec.setup_commands is not None:
        payload["setup_commands"] = [
            command.model_dump(exclude_none=True) for command in spec.setup_commands
        ]
    if spec.env is not None:
        payload["env"] = spec.env
    if spec.files is not None:
        payload["files"] = [item.model_dump() for item in spec.files]
    if spec.network is not None:
        if env_type == "self_hosted":
            raise ApiError(
                "invalid_request",
                "network needs openai_hosted",
                code="invalid_request",
            )
        dumped = spec.network.model_dump(exclude_none=True)
        try:
            session_network_from({"network": dumped})
        except SetupError as exc:
            raise ApiError(
                "invalid_request", exc.message, code="invalid_request"
            ) from exc
        if env_type == "openai_hosted":
            payload["network"] = dumped
    try:
        packages_from(payload)
        setup_commands_from(payload)
        session_env_from(payload)
        inline_files_from(payload)
        session_network_from(payload)
    except SetupError as exc:
        raise ApiError("invalid_request", exc.message, code="invalid_request") from exc
    return payload
