"""Immutable agent versions and pins on sessions and turns.

Revision ID: 0019_agent_versions
Revises: 0018_sandbox_status
Create Date: 2026-09-29
"""

import json
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0019_agent_versions"
down_revision: str | None = "0018_sandbox_status"
branch_labels: str | None = None
depends_on: str | None = None


def _json(value: object, fallback: object) -> object:
    if value is None:
        return fallback
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded
    return value


def _definition(row: Any) -> dict[str, Any]:
    metadata = _json(row["metadata"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    tools = _json(row["tools"], [])
    if not isinstance(tools, list):
        tools = []
    defaults = _json(row["session_defaults"], None)
    effort = metadata.get("apipi.thinking")
    reasoning = {"effort": effort} if isinstance(effort, str) else {"effort": None}
    return {
        "name": row["name"],
        "model": row["model"],
        "instructions": row["instructions"],
        "idle_ttl": row["idle_ttl"],
        "metadata": metadata,
        "tools": tools,
        "session_defaults": defaults,
        "reasoning": reasoning,
    }


def upgrade() -> None:
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("definition", sa.JSON(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "agent_id"],
            ["agents.tenant_id", "agents.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
        sa.UniqueConstraint("tenant_id", "agent_id", "number"),
        sa.CheckConstraint("number >= 1", name="agent_versions_number_check"),
    )
    op.create_table(
        "agent_version_activations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("from_version_id", sa.Uuid(), nullable=True),
        sa.Column("activated_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["agent_versions.tenant_id", "agent_versions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "id"),
    )
    op.add_column("agents", sa.Column("active_version_id", sa.Uuid(), nullable=True))
    op.add_column("sessions", sa.Column("agent_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "sessions", sa.Column("agent_version_number", sa.Integer(), nullable=True)
    )
    op.add_column("turns", sa.Column("agent_version_id", sa.Uuid(), nullable=True))
    op.add_column(
        "turns", sa.Column("agent_version_number", sa.Integer(), nullable=True)
    )
    bind = op.get_bind()
    agents = bind.execute(
        sa.text(
            "SELECT id, tenant_id, name, model, instructions, idle_ttl, "
            "metadata, tools, session_defaults, created_at FROM agents"
        )
    ).mappings()
    for row in agents:
        version_id = uuid.uuid4()
        created = row["created_at"]
        bind.execute(
            sa.text(
                "INSERT INTO agent_versions "
                "(id, tenant_id, agent_id, number, status, definition, source, "
                "created_by, note, created_at) "
                "VALUES (:id, :tenant_id, :agent_id, 1, 'ready', :definition, "
                "'migration', NULL, NULL, :created_at)"
            ),
            {
                "id": version_id,
                "tenant_id": row["tenant_id"],
                "agent_id": row["id"],
                "definition": json.dumps(_definition(row)),
                "created_at": created,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO agent_version_activations "
                "(id, tenant_id, agent_id, version_id, from_version_id, "
                "activated_by, created_at) "
                "VALUES (:id, :tenant_id, :agent_id, :version_id, NULL, NULL, "
                ":created_at)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": row["tenant_id"],
                "agent_id": row["id"],
                "version_id": version_id,
                "created_at": created,
            },
        )
        bind.execute(
            sa.text("UPDATE agents SET active_version_id = :version_id WHERE id = :id"),
            {"version_id": version_id, "id": row["id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE sessions SET agent_version_id = :version_id, "
                "agent_version_number = 1 WHERE agent_id = :agent_id"
            ),
            {"version_id": version_id, "agent_id": row["id"]},
        )


def downgrade() -> None:
    op.drop_column("turns", "agent_version_number")
    op.drop_column("turns", "agent_version_id")
    op.drop_column("sessions", "agent_version_number")
    op.drop_column("sessions", "agent_version_id")
    op.drop_column("agents", "active_version_id")
    op.drop_table("agent_version_activations")
    op.drop_table("agent_versions")
