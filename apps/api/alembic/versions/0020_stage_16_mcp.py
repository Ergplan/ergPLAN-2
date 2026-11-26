"""stage 16: the MCP server's access (OAuth clients and tokens, one-time upload links)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-26 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mcp_clients",
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("secret_sha256", sa.String(length=64), nullable=True),
        sa.Column("client_name", sa.String(length=200), nullable=True),
        sa.Column("redirect_uris", JSONType, nullable=False),
        sa.Column("grant_types", JSONType, nullable=False),
        sa.Column("auth_method", sa.String(length=32), nullable=False),
        sa.Column("registered_from", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "auth_method in ('none','client_secret_post','client_secret_basic')", name="mcp_clients_auth"
        ),
        sa.PrimaryKeyConstraint("client_id"),
    )
    op.create_table(
        "mcp_tokens",
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=254), nullable=False),
        sa.Column("credential", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=200), nullable=False),
        sa.Column("resource", sa.String(length=500), nullable=False),
        sa.Column("redirect_uri", sa.String(length=2000), nullable=True),
        sa.Column("code_challenge", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('code','access','refresh')", name="mcp_tokens_kind"),
        sa.ForeignKeyConstraint(["client_id"], ["mcp_clients.client_id"], name="mcp_tokens_client_fk"),
        sa.PrimaryKeyConstraint("token_sha256"),
    )
    op.create_index(op.f("ix_mcp_tokens_grant_id"), "mcp_tokens", ["grant_id"], unique=False)
    op.create_index(op.f("ix_mcp_tokens_client_id"), "mcp_tokens", ["client_id"], unique=False)
    op.create_table(
        "mcp_uploads",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor", sa.String(length=254), nullable=False),
        sa.Column("params", JSONType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", JSONType, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind in ('bill','generation')", name="mcp_uploads_kind"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256"),
    )


def downgrade() -> None:
    op.drop_table("mcp_uploads")
    op.drop_index(op.f("ix_mcp_tokens_client_id"), table_name="mcp_tokens")
    op.drop_index(op.f("ix_mcp_tokens_grant_id"), table_name="mcp_tokens")
    op.drop_table("mcp_tokens")
    op.drop_table("mcp_clients")
