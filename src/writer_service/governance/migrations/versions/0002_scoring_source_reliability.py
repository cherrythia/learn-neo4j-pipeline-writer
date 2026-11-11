"""scoring_configs.source_reliability

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rows created before this migration fall back to the default reliabilities.
    with op.batch_alter_table("scoring_configs") as batch_op:
        batch_op.add_column(sa.Column("source_reliability", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("scoring_configs") as batch_op:
        batch_op.drop_column("source_reliability")
