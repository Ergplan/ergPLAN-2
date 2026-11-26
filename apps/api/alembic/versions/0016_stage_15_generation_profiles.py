"""stage 15: generation profiles supplied by the user (solar and wind, a year, per site)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-26 10:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

J = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "generation_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("site_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("technology", sa.String(length=8), nullable=False),
        sa.Column("applies_to", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=16), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("resolution_min", sa.Integer(), nullable=False),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("blocks", sa.Integer(), nullable=False),
        sa.Column("unit_in_file", sa.String(length=16), nullable=False),
        sa.Column("basis", sa.String(length=16), nullable=False),
        sa.Column("capacity_kw", sa.Numeric(16, 3), nullable=True),
        sa.Column("series_per_kw", sa.LargeBinary(), nullable=False),
        sa.Column("annual_cuf_pct", sa.Numeric(9, 4), nullable=False),
        sa.Column("monthly_cuf_pct", J, nullable=False),
        sa.Column("method", J, nullable=False),
        sa.Column("created_by", sa.String(length=254), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("withdrawn_by", sa.String(length=254), nullable=True),
        sa.CheckConstraint("technology in ('solar','wind')", name="generation_profiles_technology"),
        sa.CheckConstraint("applies_to in ('open_access','rooftop')", name="generation_profiles_applies_to"),
        sa.CheckConstraint("provenance in ('measured','stated')", name="generation_profiles_provenance"),
        sa.CheckConstraint("resolution_min in (15, 60)", name="generation_profiles_resolution"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], name="generation_profiles_site_fk"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_generation_profiles_site_id", "generation_profiles", ["site_id"])
    op.create_index("ix_generation_profiles_sha256", "generation_profiles", ["sha256"])
    op.create_index(
        "generation_profiles_site_version", "generation_profiles", ["site_id", "version"], unique=True
    )


def downgrade() -> None:
    op.drop_index("generation_profiles_site_version", table_name="generation_profiles")
    op.drop_index("ix_generation_profiles_sha256", table_name="generation_profiles")
    op.drop_index("ix_generation_profiles_site_id", table_name="generation_profiles")
    op.drop_table("generation_profiles")
