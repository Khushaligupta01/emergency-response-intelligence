from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from geoalchemy2.elements import WKBElement
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base


class ResourceType(Base):
    __tablename__ = "resource_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    unit: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    balances: Mapped[list["InventoryBalance"]] = relationship(
        back_populates="resource_type",
        passive_deletes=True,
    )
    transactions: Mapped[list["InventoryTransaction"]] = relationship(
        back_populates="resource_type",
        passive_deletes=True,
    )

    __table_args__ = (
        Index(
            "uq_resource_types_name_normalized",
            func.lower(func.btrim(name)),
            unique=True,
        ),
        Index("uq_resource_types_code", "code", unique=True),
        CheckConstraint("length(trim(name)) > 0", name="ck_resource_types_name_nonempty"),
        CheckConstraint("length(trim(code)) > 0", name="ck_resource_types_code_nonempty"),
        CheckConstraint("length(trim(unit)) > 0", name="ck_resource_types_unit_nonempty"),
        CheckConstraint("length(trim(category)) > 0", name="ck_resource_types_category_nonempty"),
    )

    @validates("code", "name", "unit", "category")
    def strip_required_text(self, key: str, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{key} cannot be blank.")
        return normalized


class ResourceDepot(Base):
    __tablename__ = "resource_depots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str | None] = mapped_column(String(500))
    district: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(100))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    geometry: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    contact_name: Mapped[str | None] = mapped_column(String(100))
    contact_phone: Mapped[str | None] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    balances: Mapped[list["InventoryBalance"]] = relationship(
        back_populates="depot",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_resource_depots_geometry", "geometry", postgresql_using="gist"),
        CheckConstraint("length(trim(name)) > 0", name="ck_resource_depots_name_nonempty"),
    )

    @validates("name")
    def strip_name(self, _key: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Depot name cannot be blank.")
        return value.strip()


class InventoryBalance(Base):
    """Current on-hand and reserved quantities, maintained with ledger entries."""

    __tablename__ = "inventory_balances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    depot_id: Mapped[int] = mapped_column(
        ForeignKey("resource_depots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    resource_type_id: Mapped[int] = mapped_column(
        ForeignKey("resource_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    reserved_quantity: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    depot: Mapped[ResourceDepot] = relationship(back_populates="balances")
    resource_type: Mapped[ResourceType] = relationship(back_populates="balances")

    @property
    def available_quantity(self) -> Decimal:
        return self.quantity - self.reserved_quantity

    __table_args__ = (
        Index(
            "uq_inventory_balances_depot_resource",
            "depot_id",
            "resource_type_id",
            unique=True,
        ),
        CheckConstraint("quantity >= 0", name="ck_inventory_balances_quantity_nonnegative"),
        CheckConstraint(
            "reserved_quantity >= 0",
            name="ck_inventory_balances_reserved_nonnegative",
        ),
        CheckConstraint(
            "reserved_quantity <= quantity",
            name="ck_inventory_balances_reserved_lte_quantity",
        ),
    )


class InventoryTransaction(Base):
    """Append-only inventory movement record; transfers link source and destination."""

    __tablename__ = "inventory_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_type_id: Mapped[int] = mapped_column(
        ForeignKey("resource_types.id", ondelete="RESTRICT"),
        nullable=False,
    )
    depot_id: Mapped[int] = mapped_column(
        ForeignKey("resource_depots.id", ondelete="RESTRICT"),
        nullable=False,
    )
    destination_depot_id: Mapped[int | None] = mapped_column(
        ForeignKey("resource_depots.id", ondelete="RESTRICT"),
        nullable=True,
    )
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    source: Mapped[str | None] = mapped_column(String(100))
    reference: Mapped[str | None] = mapped_column(String(150))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(150))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    resource_type: Mapped[ResourceType] = relationship(back_populates="transactions")
    depot: Mapped[ResourceDepot] = relationship(foreign_keys=[depot_id])
    destination_depot: Mapped[ResourceDepot | None] = relationship(
        foreign_keys=[destination_depot_id]
    )

    __table_args__ = (
        Index("ix_inventory_transactions_depot_resource_time", "depot_id", "resource_type_id", "occurred_at"),
        Index(
            "ix_inventory_transactions_destination_time",
            "destination_depot_id",
            "occurred_at",
        ),
        CheckConstraint(
            "transaction_type IN ('receipt', 'consumption', 'transfer', 'opening_balance')",
            name="ck_inventory_transactions_type",
        ),
        CheckConstraint("quantity > 0", name="ck_inventory_transactions_quantity_positive"),
        CheckConstraint(
            "(transaction_type = 'transfer' AND destination_depot_id IS NOT NULL "
            "AND destination_depot_id <> depot_id) OR "
            "(transaction_type <> 'transfer' AND destination_depot_id IS NULL)",
            name="ck_inventory_transactions_transfer_endpoints",
        ),
    )

# Retain the original public model name for code that imported Inventory.
Inventory = InventoryBalance


@event.listens_for(InventoryTransaction, "before_update")
def prevent_inventory_transaction_update(_mapper, _connection, _target) -> None:
    raise ValueError("Inventory transactions are immutable audit records.")


@event.listens_for(InventoryTransaction, "before_delete")
def prevent_inventory_transaction_delete(_mapper, _connection, _target) -> None:
    raise ValueError("Inventory transactions are immutable audit records.")
