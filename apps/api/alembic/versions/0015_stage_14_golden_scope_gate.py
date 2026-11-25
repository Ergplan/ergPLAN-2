"""stage 14: the approval gate counts the golden tests that apply to the version

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-25 22:00:00

"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL = Path(__file__).resolve().parents[2] / "ergplan/db/sql"


def upgrade() -> None:
    op.execute((SQL / "0015_golden_scope_gate.sql").read_text())


def downgrade() -> None:
    # the gate as Stage 1 wrote it: every test of the family and jurisdiction counts
    src = (SQL / "0002_rulebook_triggers.sql").read_text()
    start = src.index("CREATE OR REPLACE FUNCTION ergplan_version_gate()")
    op.execute(src[start : src.index("CREATE TRIGGER rulebook_versions_gate")])
    op.execute("DROP FUNCTION IF EXISTS ergplan_test_applies(jsonb, date, date, text)")
