"""review_items.reason: add ``duplicate`` (possible-duplicate entities, #38)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("score", "conflict")
NEW = ("score", "conflict", "duplicate")


CHECK = "ck_review_items_review_reason"


def _reason(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(
        *values, name="review_reason", native_enum=False, create_constraint=True, length=20
    )


def _set_reasons(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite can't alter a CHECK: batch mode rebuilds the table with the new one.
        with op.batch_alter_table("review_items") as batch_op:
            batch_op.alter_column(
                "reason", existing_type=_reason(old), type_=_reason(new), existing_nullable=False
            )
        return
    # MySQL: alter_column would add a second CHECK of the same name; replace it instead.
    op.drop_constraint(op.f(CHECK), "review_items", type_="check")
    allowed = ", ".join(f"'{v}'" for v in new)
    op.create_check_constraint(op.f(CHECK), "review_items", f"reason IN ({allowed})")


def upgrade() -> None:
    _set_reasons(OLD, NEW)


def downgrade() -> None:
    # Older code can't show a duplicate item: they become ordinary score items.
    op.execute("UPDATE review_items SET reason = 'score' WHERE reason = 'duplicate'")
    _set_reasons(NEW, OLD)
