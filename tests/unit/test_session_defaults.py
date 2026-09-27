import uuid

import pytest

from apipi.env.spec import EnvironmentSpec, environment_payload
from apipi.gateway.errors import ApiError
from apipi.services.session_defaults import (
    merge_session_create,
    normalize_sandbox_aliases,
    sandbox_defaults_from_metadata,
)
from apipi.worker.pi.sandbox import resolve_sandbox_image, resolve_sandbox_size


def _env(**fields: object) -> EnvironmentSpec:
    body: dict[str, object] = {"type": "openai_hosted"}
    body.update(fields)
    return EnvironmentSpec.model_validate(body)


def test_env_merges_by_key_and_session_wins() -> None:
    merged, vaults, _size, _image = merge_session_create(
        agent_defaults={
            "environment": {"type": "openai_hosted", "env": {"A": "1", "B": "2"}}
        },
        environment=_env(env={"B": "9", "C": "3"}),
        vault_ids=None,
        inherit=True,
    )
    assert merged is not None
    assert merged.env == {"A": "1", "B": "9", "C": "3"}
    assert vaults is None


def test_packages_replace_per_ecosystem() -> None:
    merged, _, _, _ = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "packages": {"python": ["httpx"], "npm": ["typescript"]},
            }
        },
        environment=_env(packages={"python": ["requests"]}),
        vault_ids=None,
        inherit=True,
    )
    assert merged is not None
    assert merged.packages is not None
    assert merged.packages.python == ["requests"]
    assert merged.packages.npm == ["typescript"]


def test_files_merge_by_path_and_skills_union() -> None:
    merged, vaults, _, _ = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "files": [
                    {"type": "inline", "path": "a.txt", "data": "YQ=="},
                    {"type": "inline", "path": "b.txt", "data": "Yg=="},
                ],
                "skills": [{"type": "skill_reference", "skill_id": "skill_a"}],
            },
            "vault_ids": ["11111111-1111-1111-1111-111111111111"],
        },
        environment=_env(
            files=[{"type": "inline", "path": "b.txt", "data": "Yw=="}],
            skills=[{"type": "skill_reference", "skill_id": "skill_a"}],
        ),
        vault_ids=[uuid.UUID("11111111-1111-1111-1111-111111111111")],
        inherit=True,
    )
    assert merged is not None
    assert merged.files is not None
    assert [item.path for item in merged.files] == ["a.txt", "b.txt"]
    replaced = merged.files[1]
    assert replaced.type == "inline"
    assert replaced.data == "Yw=="
    assert merged.skills is not None
    assert [item.skill_id for item in merged.skills] == ["skill_a"]
    assert vaults == [uuid.UUID("11111111-1111-1111-1111-111111111111")]


def test_lists_and_network_replace() -> None:
    merged, _, _, _ = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "capability_directories": ["/old"],
                "setup_commands": [{"command": "echo old"}],
                "network": {"access": "disabled"},
            }
        },
        environment=_env(
            capability_directories=["/new"],
            setup_commands=[{"command": "echo new"}],
            network={"access": "restricted", "allowed_domains": ["example.com"]},
        ),
        vault_ids=None,
        inherit=True,
    )
    assert merged is not None
    assert merged.capability_directories == ["/new"]
    assert merged.setup_commands is not None
    assert merged.setup_commands[0].command == "echo new"
    assert merged.network is not None
    assert merged.network.access == "restricted"


def test_type_mismatch_keeps_session_environment() -> None:
    merged, vaults, size, image = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "env": {"TOKEN": "x"},
                "sandbox_size": "L",
                "sandbox_image": "browser",
            },
            "vault_ids": ["11111111-1111-1111-1111-111111111111"],
        },
        environment=EnvironmentSpec(type="none"),
        vault_ids=None,
        inherit=True,
    )
    assert merged is not None
    assert merged.type == "none"
    assert merged.env is None
    assert size == "L"
    assert image == "browser"
    assert vaults == [uuid.UUID("11111111-1111-1111-1111-111111111111")]


def test_omitted_environment_uses_agent_type() -> None:
    merged, _, size, _ = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "env": {"A": "1"},
                "sandbox_size": "M",
            }
        },
        environment=None,
        vault_ids=None,
        inherit=True,
    )
    assert merged is not None
    assert merged.type == "openai_hosted"
    assert merged.env == {"A": "1"}
    assert merged.sandbox_size is None
    assert size == "M"


def test_inherit_false_ignores_defaults() -> None:
    merged, vaults, size, image = merge_session_create(
        agent_defaults={
            "environment": {
                "type": "openai_hosted",
                "env": {"A": "1"},
                "sandbox_size": "L",
            },
            "vault_ids": ["11111111-1111-1111-1111-111111111111"],
        },
        environment=_env(env={"B": "2"}),
        vault_ids=None,
        inherit=False,
    )
    assert merged is not None
    assert merged.env == {"B": "2"}
    assert vaults is None
    assert size is None
    assert image is None


def test_merged_skill_limit() -> None:
    skills = [
        {"type": "skill_reference", "skill_id": f"skill_{index}"} for index in range(32)
    ]
    merged, _, _, _ = merge_session_create(
        agent_defaults={"environment": {"type": "openai_hosted", "skills": skills}},
        environment=_env(
            skills=[{"type": "skill_reference", "skill_id": "skill_extra"}]
        ),
        vault_ids=None,
        inherit=True,
    )
    with pytest.raises(ApiError, match="at most 32"):
        environment_payload(merged)


def test_alias_conflict() -> None:
    with pytest.raises(ApiError, match="sandbox_size"):
        normalize_sandbox_aliases(
            {"apipi.sandbox_size": "S"},
            {"environment": {"type": "openai_hosted", "sandbox_size": "L"}},
        )


def test_alias_copies_metadata_into_defaults() -> None:
    meta, defaults = normalize_sandbox_aliases(
        {"apipi.sandbox_image": "browser", "keep": "me"},
        None,
    )
    assert meta is not None
    assert meta["keep"] == "me"
    assert defaults is not None
    assert defaults["environment"]["sandbox_image"] == "browser"
    assert defaults["environment"]["type"] == "openai_hosted"


def test_metadata_copy_for_migration() -> None:
    copied = sandbox_defaults_from_metadata(
        {"apipi.sandbox_size": "M", "apipi.sandbox_image": "browser", "other": 1}
    )
    assert copied == {
        "environment": {
            "type": "openai_hosted",
            "sandbox_size": "M",
            "sandbox_image": "browser",
        }
    }
    assert sandbox_defaults_from_metadata({"other": 1}) is None


def test_sandbox_resolution_order() -> None:
    size = resolve_sandbox_size(
        environment_size=None,
        session_metadata={"apipi.sandbox_size": "M"},
        agent_default="L",
        agent_metadata={"apipi.sandbox_size": "S"},
        default="S",
    )
    assert size == "M"
    image = resolve_sandbox_image(
        environment_image=None,
        session_metadata=None,
        agent_default="browser",
        agent_metadata={"apipi.sandbox_image": "default"},
        size="M",
        default="default",
    )
    assert image == "browser"
    legacy = resolve_sandbox_size(
        environment_size=None,
        session_metadata=None,
        agent_default=None,
        agent_metadata={"apipi.sandbox_size": "L"},
        default="S",
    )
    assert legacy == "L"
