"""Transactional inventory movements backed by an auditable movement ledger."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.resource import InventoryBalance, InventoryTransaction

MovementType = Literal["receipt", "consumption", "transfer"]


class InsufficientInventoryError(ValueError):
    """Raised when an issue or transfer would make available stock negative."""


def _positive_quantity(value: Decimal | int | str) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Quantity must be a finite positive number.") from error
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("Quantity must be a finite positive number.")
    if quantity > Decimal("999999999999.99"):
        raise ValueError("Quantity exceeds the supported inventory precision.")
    try:
        representable_quantity = quantity.quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Quantity exceeds the supported inventory precision.") from error
    if representable_quantity != quantity:
        raise ValueError("Quantity cannot have more than two decimal places.")
    return quantity


def _insert_missing_balances(
    session: Session,
    resource_type_id: int,
    depot_ids: list[int],
) -> None:
    values = [
        {
            "depot_id": depot_id,
            "resource_type_id": resource_type_id,
            "quantity": Decimal("0"),
            "reserved_quantity": Decimal("0"),
        }
        for depot_id in sorted(set(depot_ids))
    ]
    if not values:
        return

    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(InventoryBalance).values(values)
    elif dialect == "sqlite":
        statement = sqlite_insert(InventoryBalance).values(values)
    else:
        raise RuntimeError(f"Inventory service does not support database dialect {dialect!r}.")
    session.execute(
        statement.on_conflict_do_nothing(
            index_elements=["depot_id", "resource_type_id"]
        )
    )


def record_stock_movement(
    session: Session,
    *,
    resource_type_id: int,
    depot_id: int,
    transaction_type: MovementType,
    quantity: Decimal | int | str,
    destination_depot_id: int | None = None,
    source: str | None = None,
    reference: str | None = None,
    notes: str | None = None,
    created_by: str | None = None,
) -> InventoryTransaction:
    """Record a receipt, consumption, or atomic inter-depot transfer.

    The caller owns the surrounding SQLAlchemy transaction and should commit
    the returned record together with the balance changes.
    """
    if transaction_type not in ("receipt", "consumption", "transfer"):
        raise ValueError(f"Unsupported inventory movement type: {transaction_type!r}.")
    amount = _positive_quantity(quantity)
    if transaction_type == "transfer":
        if destination_depot_id is None:
            raise ValueError("Transfers require a destination depot.")
        if destination_depot_id == depot_id:
            raise ValueError("Transfer source and destination depots must differ.")
        required_depot_ids = [depot_id, destination_depot_id]
    else:
        if destination_depot_id is not None:
            raise ValueError("Only transfers may specify a destination depot.")
        required_depot_ids = [depot_id]

    _insert_missing_balances(session, resource_type_id, required_depot_ids)
    balances = session.scalars(
        select(InventoryBalance)
        .where(
            InventoryBalance.resource_type_id == resource_type_id,
            InventoryBalance.depot_id.in_(required_depot_ids),
        )
        .order_by(InventoryBalance.depot_id)
        .with_for_update()
    ).all()
    balance_by_depot = {balance.depot_id: balance for balance in balances}
    if len(balance_by_depot) != len(set(required_depot_ids)):
        raise ValueError("A referenced depot or resource type does not exist.")

    source_balance = balance_by_depot[depot_id]
    if transaction_type in ("consumption", "transfer"):
        if source_balance.available_quantity < amount:
            raise InsufficientInventoryError(
                f"Depot {depot_id} has {source_balance.available_quantity} available; "
                f"{amount} requested."
            )

    if transaction_type == "receipt":
        source_balance.quantity += amount
    elif transaction_type == "consumption":
        source_balance.quantity -= amount
    else:
        source_balance.quantity -= amount
        balance_by_depot[destination_depot_id].quantity += amount

    movement = InventoryTransaction(
        resource_type_id=resource_type_id,
        depot_id=depot_id,
        destination_depot_id=destination_depot_id,
        transaction_type=transaction_type,
        quantity=amount,
        source=source,
        reference=reference,
        notes=notes,
        created_by=created_by,
    )
    session.add(movement)
    session.flush()
    return movement


def calculate_inventory_balance(
    session: Session,
    *,
    resource_type_id: int,
    depot_id: int,
) -> Decimal:
    """Reconstruct on-hand balance from ledger entries for auditing."""
    signed_quantity = case(
        (
            InventoryTransaction.transaction_type.in_(("receipt", "opening_balance")),
            InventoryTransaction.quantity,
        ),
        (
            InventoryTransaction.transaction_type == "consumption",
            -InventoryTransaction.quantity,
        ),
        (
            (InventoryTransaction.transaction_type == "transfer")
            & (InventoryTransaction.depot_id == depot_id),
            -InventoryTransaction.quantity,
        ),
        (
            (InventoryTransaction.transaction_type == "transfer")
            & (InventoryTransaction.destination_depot_id == depot_id),
            InventoryTransaction.quantity,
        ),
        else_=Decimal("0"),
    )
    result = session.scalar(
        select(func.coalesce(func.sum(signed_quantity), 0)).where(
            InventoryTransaction.resource_type_id == resource_type_id,
            or_(
                InventoryTransaction.depot_id == depot_id,
                InventoryTransaction.destination_depot_id == depot_id,
            ),
        )
    )
    return Decimal(result)
