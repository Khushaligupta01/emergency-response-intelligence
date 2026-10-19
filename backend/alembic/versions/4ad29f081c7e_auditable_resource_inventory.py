"""add auditable resource inventory and preserve existing balances

Revision ID: 4ad29f081c7e
Revises: 8f149080cc9b
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry


revision: str = "4ad29f081c7e"
down_revision: Union[str, Sequence[str], None] = "8f149080cc9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resource_types",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column(
        "resource_types",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "resource_types",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint(
        "ck_resource_types_name_nonempty",
        "resource_types",
        "length(trim(name)) > 0",
    )
    op.create_check_constraint(
        "ck_resource_types_code_nonempty",
        "resource_types",
        "length(trim(code)) > 0",
    )
    op.create_check_constraint(
        "ck_resource_types_unit_nonempty",
        "resource_types",
        "length(trim(unit)) > 0",
    )
    op.create_check_constraint(
        "ck_resource_types_category_nonempty",
        "resource_types",
        "length(trim(category)) > 0",
    )
    op.create_index(
        "uq_resource_types_name_normalized",
        "resource_types",
        [sa.text("lower(btrim(name))")],
        unique=True,
    )

    op.alter_column(
        "resource_depots",
        "geometry",
        existing_type=Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    op.add_column("resource_depots", sa.Column("district", sa.String(length=100), nullable=True))
    op.add_column("resource_depots", sa.Column("state", sa.String(length=100), nullable=True))
    op.add_column("resource_depots", sa.Column("postal_code", sa.String(length=20), nullable=True))
    op.add_column(
        "resource_depots",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_check_constraint(
        "ck_resource_depots_name_nonempty",
        "resource_depots",
        "length(trim(name)) > 0",
    )

    op.create_table(
        "inventory_balances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "depot_id",
            sa.Integer(),
            sa.ForeignKey("resource_depots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "resource_type_id",
            sa.Integer(),
            sa.ForeignKey("resource_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_quantity", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_inventory_balances_quantity_nonnegative"),
        sa.CheckConstraint(
            "reserved_quantity >= 0",
            name="ck_inventory_balances_reserved_nonnegative",
        ),
        sa.CheckConstraint(
            "reserved_quantity <= quantity",
            name="ck_inventory_balances_reserved_lte_quantity",
        ),
    )
    op.create_index(
        "uq_inventory_balances_depot_resource",
        "inventory_balances",
        ["depot_id", "resource_type_id"],
        unique=True,
    )

    op.create_table(
        "inventory_transactions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "resource_type_id",
            sa.Integer(),
            sa.ForeignKey("resource_types.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "depot_id",
            sa.Integer(),
            sa.ForeignKey("resource_depots.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "destination_depot_id",
            sa.Integer(),
            sa.ForeignKey("resource_depots.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("transaction_type", sa.String(length=30), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 2), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("reference", sa.String(length=150), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=150), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "transaction_type IN ('receipt', 'consumption', 'transfer', 'opening_balance')",
            name="ck_inventory_transactions_type",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_inventory_transactions_quantity_positive"),
        sa.CheckConstraint(
            "(transaction_type = 'transfer' AND destination_depot_id IS NOT NULL "
            "AND destination_depot_id <> depot_id) OR "
            "(transaction_type <> 'transfer' AND destination_depot_id IS NULL)",
            name="ck_inventory_transactions_transfer_endpoints",
        ),
    )
    op.create_index(
        "ix_inventory_transactions_depot_resource_time",
        "inventory_transactions",
        ["depot_id", "resource_type_id", "occurred_at"],
    )
    op.create_index(
        "ix_inventory_transactions_destination_time",
        "inventory_transactions",
        ["destination_depot_id", "occurred_at"],
    )

    # Preserve all existing materialized quantities and record the legacy stock as
    # explicit opening entries so the new ledger and balance start consistently.
    op.execute(
        """
        INSERT INTO inventory_balances
            (id, depot_id, resource_type_id, quantity, reserved_quantity, updated_at)
        SELECT id, depot_id, resource_type_id, quantity, reserved_quantity, updated_at
        FROM inventory
        """
    )
    op.execute(
        """
        INSERT INTO inventory_transactions
            (resource_type_id, depot_id, transaction_type, quantity, source, reference, occurred_at, created_at)
        SELECT resource_type_id, depot_id, 'opening_balance', quantity,
               'legacy_inventory_migration',
               'inventory.id=' || id::text,
               updated_at,
               updated_at
        FROM inventory
        WHERE quantity > 0
        """
    )
    op.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('inventory_balances', 'id'),
            COALESCE((SELECT MAX(id) FROM inventory_balances), 1),
            EXISTS (SELECT 1 FROM inventory_balances)
        )
        """
    )
    op.drop_index("uq_inventory_depot_resource", table_name="inventory")
    op.drop_table("inventory")


def downgrade() -> None:
    op.create_table(
        "inventory",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "depot_id",
            sa.Integer(),
            sa.ForeignKey("resource_depots.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "resource_type_id",
            sa.Integer(),
            sa.ForeignKey("resource_types.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved_quantity", sa.Numeric(14, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_inventory_quantity_nonnegative"),
        sa.CheckConstraint("reserved_quantity >= 0", name="ck_inventory_reserved_nonnegative"),
        sa.CheckConstraint(
            "reserved_quantity <= quantity",
            name="ck_inventory_reserved_lte_quantity",
        ),
    )
    op.create_index(
        "uq_inventory_depot_resource",
        "inventory",
        ["depot_id", "resource_type_id"],
        unique=True,
    )
    op.execute(
        """
        INSERT INTO inventory (id, depot_id, resource_type_id, quantity, reserved_quantity, updated_at)
        SELECT id, depot_id, resource_type_id, quantity, reserved_quantity, updated_at
        FROM inventory_balances
        """
    )
    op.execute(
        """
        SELECT setval(
            pg_get_serial_sequence('inventory', 'id'),
            COALESCE((SELECT MAX(id) FROM inventory), 1),
            EXISTS (SELECT 1 FROM inventory)
        )
        """
    )

    op.drop_index("ix_inventory_transactions_destination_time", table_name="inventory_transactions")
    op.drop_index("ix_inventory_transactions_depot_resource_time", table_name="inventory_transactions")
    op.drop_table("inventory_transactions")
    op.drop_index("uq_inventory_balances_depot_resource", table_name="inventory_balances")
    op.drop_table("inventory_balances")

    op.drop_constraint("ck_resource_depots_name_nonempty", "resource_depots", type_="check")
    op.drop_column("resource_depots", "updated_at")
    op.drop_column("resource_depots", "postal_code")
    op.drop_column("resource_depots", "state")
    op.drop_column("resource_depots", "district")
    op.alter_column(
        "resource_depots",
        "geometry",
        existing_type=Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )

    op.drop_index("uq_resource_types_name_normalized", table_name="resource_types")
    op.drop_constraint("ck_resource_types_category_nonempty", "resource_types", type_="check")
    op.drop_constraint("ck_resource_types_unit_nonempty", "resource_types", type_="check")
    op.drop_constraint("ck_resource_types_code_nonempty", "resource_types", type_="check")
    op.drop_constraint("ck_resource_types_name_nonempty", "resource_types", type_="check")
    op.drop_column("resource_types", "updated_at")
    op.drop_column("resource_types", "created_at")
    op.drop_column("resource_types", "is_active")
