from apipi.config import Settings
from apipi.services.agent_versions import NON_VERSIONED_FIELDS, VERSIONED_FIELDS
from apipi.services.agents import AgentWrite


def test_every_write_field_is_versioned_or_listed() -> None:
    fields = set(AgentWrite.model_fields)
    assert fields == VERSIONED_FIELDS | NON_VERSIONED_FIELDS
    assert not VERSIONED_FIELDS & NON_VERSIONED_FIELDS


def test_version_retention_defaults_to_ten() -> None:
    assert Settings().agent_versions_keep == 10
