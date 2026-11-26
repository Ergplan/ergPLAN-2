"""stage 15: transition plans (the target and the runs that make the phases)

Revision ID: 0019
Revises: 0015
Create Date: 2026-11-26 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "transition_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("target", JSONType, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps", JSONType, nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status in ('running','done','failed')", name="transition_plans_status"),
        sa.ForeignKeyConstraint(["connection_id"], ["connections.id"], name="transition_plans_connection_fk"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_transition_plans_connection_id"), "transition_plans", ["connection_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transition_plans_connection_id"), table_name="transition_plans")
    op.drop_table("transition_plans")
