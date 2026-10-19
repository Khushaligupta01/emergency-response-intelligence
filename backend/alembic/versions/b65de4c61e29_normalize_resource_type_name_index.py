"""normalize resource type name uniqueness for PostgreSQL introspection

Revision ID: b65de4c61e29
Revises: 4ad29f081c7e
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b65de4c61e29"
down_revision: Union[str, Sequence[str], None] = "4ad29f081c7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("uq_resource_types_name_normalized", table_name="resource_types")
    op.create_index(
        "uq_resource_types_name_normalized",
        "resource_types",
        [sa.text("lower(btrim(name))")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_resource_types_name_normalized", table_name="resource_types")
    op.create_index(
        "uq_resource_types_name_normalized",
        "resource_types",
        [sa.text("lower(trim(name))")],
        unique=True,
    )
