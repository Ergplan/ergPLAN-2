"""stage 15: what the client states about a site that its bills do not say

Revision ID: 0017
Revises: 0015
Create Date: 2026-11-26 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_answers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=32), nullable=False),
        sa.Column("value", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("stated_by", sa.String(length=254), nullable=False),
        sa.Column("stated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by", sa.String(length=254), nullable=True),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], name="site_answers_site_fk"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_site_answers_site_id", "site_answers", ["site_id"], unique=False)
    op.create_index(
        "site_answers_one_open",
        "site_answers",
        ["site_id", "key"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("site_answers_one_open", table_name="site_answers")
    op.drop_index("ix_site_answers_site_id", table_name="site_answers")
    op.drop_table("site_answers")
