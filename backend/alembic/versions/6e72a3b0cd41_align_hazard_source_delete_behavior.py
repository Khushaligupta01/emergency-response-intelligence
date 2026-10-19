"""preserve hazard observations when a source registry row is removed

Revision ID: 6e72a3b0cd41
Revises: 92f17be4a63c
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op


revision: str = "6e72a3b0cd41"
down_revision: Union[str, Sequence[str], None] = "92f17be4a63c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "hazard_observations_source_id_fkey",
        "hazard_observations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "hazard_observations_source_id_fkey",
        "hazard_observations",
        "disaster_sources",
        ["source_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "hazard_observations_source_id_fkey",
        "hazard_observations",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "hazard_observations_source_id_fkey",
        "hazard_observations",
        "disaster_sources",
        ["source_id"],
        ["id"],
        ondelete="CASCADE",
    )
