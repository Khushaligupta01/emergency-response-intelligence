"""allow hazard geometries without a precise observation timestamp

Revision ID: 92f17be4a63c
Revises: f417c6d8b312
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "92f17be4a63c"
down_revision: Union[str, Sequence[str], None] = "f417c6d8b312"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "hazard_observations",
        "observed_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "hazard_observations",
        "observed_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
