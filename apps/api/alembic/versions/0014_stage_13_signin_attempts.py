"""stage 13 sign-in attempts

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-25 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "signin_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("address", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=254), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("outcome in ('ok','wrong','locked','off')", name="signin_attempts_outcome"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_signin_attempts_address_at", "signin_attempts", ["address", "at"], unique=False)
    # One administrator account is the only password from here on (set from the server's environment
    # when the API starts). The hashes of the older per-person passwords are left where they are:
    # nothing checks them any more (`platform.admin.check_password` accepts the administrator only),
    # and dropping them cannot be undone, which is the person's to decide
    # (docs/questions-for-review.md).


def downgrade() -> None:
    op.drop_index("ix_signin_attempts_address_at", table_name="signin_attempts")
    op.drop_table("signin_attempts")
