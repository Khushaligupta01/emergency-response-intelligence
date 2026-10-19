from decimal import Decimal

import pytest
from geoalchemy2 import Geometry
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.resource import (
    InventoryBalance,
    InventoryTransaction,
    ResourceDepot,
    ResourceType,
)
from app.services.resource_inventory import (
    InsufficientInventoryError,
    calculate_inventory_balance,
    record_stock_movement,
)


@compiles(Geometry, "sqlite")
def compile_geometry_for_sqlite(_type, _compiler, **_kwargs):
    return "BLOB"


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.create_function("RecoverGeometryColumn", 5, lambda *_args: 1)
        connection.create_function("GeomFromEWKT", 1, lambda value: value)
        connection.create_function(
            "btrim",
            1,
            lambda value: value.strip() if value is not None else None,
            deterministic=True,
        )
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(
        engine,
        tables=[
            ResourceType.__table__,
            ResourceDepot.__table__,
            InventoryBalance.__table__,
            InventoryTransaction.__table__,
        ],
    )
    with Session(engine) as db:
        yield db
        db.rollback()
    engine.dispose()


@pytest.fixture()
def inventory_setup(session):
    resource_type = ResourceType(
        code="WATER-20L",
        name="Drinking Water",
        category="water",
        unit="container",
        description="20-litre drinking water container",
    )
    depot = ResourceDepot(
        name="Patna Central Depot",
        address="Patna, Bihar",
        district="Patna",
        state="Bihar",
    )
    second_depot = ResourceDepot(name="Gaya Depot", district="Gaya", state="Bihar")
    session.add_all([resource_type, depot, second_depot])
    session.flush()
    return resource_type, depot, second_depot


def test_creates_data_driven_resource_type_and_depot(session, inventory_setup):
    resource_type, depot, _ = inventory_setup

    assert resource_type.category == "water"
    assert resource_type.unit == "container"
    assert resource_type.is_active is True
    assert depot.district == "Patna"
    assert depot.geometry is None
    assert depot.is_active is True


def test_rejects_duplicate_normalized_resource_type_name(session, inventory_setup):
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                ResourceType(
                    code="POTABLE-WATER",
                    name="  drinking WATER ",
                    category="water",
                    unit="litre",
                )
            )
            session.flush()


def test_receipt_and_consumption_update_balance_and_ledger(session, inventory_setup):
    resource_type, depot, _ = inventory_setup

    receipt = record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="receipt",
        quantity="100.00",
        source="District warehouse",
        reference="DELIVERY-42",
    )
    issue = record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="consumption",
        quantity=Decimal("32.50"),
        source="Relief distribution",
        reference="DISTRIBUTION-7",
    )
    balance = session.scalar(
        select(InventoryBalance).where(
            InventoryBalance.depot_id == depot.id,
            InventoryBalance.resource_type_id == resource_type.id,
        )
    )

    assert receipt.id is not None
    assert issue.id is not None
    assert balance.quantity == Decimal("67.50")
    assert balance.available_quantity == Decimal("67.50")
    assert calculate_inventory_balance(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
    ) == Decimal("67.50")
    assert session.scalar(select(InventoryTransaction).where(
        InventoryTransaction.reference == "DELIVERY-42"
    )).source == "District warehouse"


def test_rejects_consumption_that_would_make_stock_negative(session, inventory_setup):
    resource_type, depot, _ = inventory_setup
    record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="receipt",
        quantity=10,
    )

    with pytest.raises(InsufficientInventoryError):
        record_stock_movement(
            session,
            resource_type_id=resource_type.id,
            depot_id=depot.id,
            transaction_type="consumption",
            quantity=Decimal("10.01"),
        )

    balance = session.scalar(select(InventoryBalance).where(InventoryBalance.depot_id == depot.id))
    assert balance.quantity == Decimal("10.00")
    assert session.scalar(select(InventoryTransaction).where(
        InventoryTransaction.transaction_type == "consumption"
    )) is None


def test_transfer_is_atomic_and_updates_both_depots(session, inventory_setup):
    resource_type, depot, second_depot = inventory_setup
    record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="receipt",
        quantity=75,
    )
    transfer = record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        destination_depot_id=second_depot.id,
        transaction_type="transfer",
        quantity=25,
        reference="TRANSFER-9",
    )

    balances = {
        row.depot_id: row.quantity
        for row in session.scalars(
            select(InventoryBalance).where(
                InventoryBalance.resource_type_id == resource_type.id
            )
        )
    }
    assert transfer.destination_depot_id == second_depot.id
    assert balances == {depot.id: Decimal("50.00"), second_depot.id: Decimal("25.00")}
    assert calculate_inventory_balance(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
    ) == Decimal("50.00")
    assert calculate_inventory_balance(
        session,
        resource_type_id=resource_type.id,
        depot_id=second_depot.id,
    ) == Decimal("25.00")


def test_rejects_invalid_references_and_non_positive_quantity(session, inventory_setup):
    resource_type, depot, _ = inventory_setup

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            record_stock_movement(
                session,
                resource_type_id=resource_type.id,
                depot_id=999999,
                transaction_type="receipt",
                quantity=1,
            )
            session.flush()

    for quantity in (0, -1, "NaN", "Infinity"):
        with pytest.raises(ValueError, match="positive"):
            record_stock_movement(
                session,
                resource_type_id=resource_type.id,
                depot_id=depot.id,
                transaction_type="receipt",
                quantity=quantity,
            )

    with pytest.raises(ValueError, match="decimal places"):
        record_stock_movement(
            session,
            resource_type_id=resource_type.id,
            depot_id=depot.id,
            transaction_type="receipt",
            quantity="1.001",
            source="TEST/DEMO",
        )

    with pytest.raises(ValueError, match="destination"):
        record_stock_movement(
            session,
            resource_type_id=resource_type.id,
            depot_id=depot.id,
            transaction_type="transfer",
            quantity=1,
        )

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            record_stock_movement(
                session,
                resource_type_id=999999,
                depot_id=depot.id,
                transaction_type="receipt",
                quantity=1,
            )


def test_database_constraints_prevent_negative_balances_and_bad_transactions(session, inventory_setup):
    resource_type, depot, _ = inventory_setup
    balance = InventoryBalance(
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        quantity=Decimal("-1"),
        reserved_quantity=Decimal("0"),
    )
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(balance)
            session.flush()

    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add(
                InventoryTransaction(
                    resource_type_id=resource_type.id,
                    depot_id=depot.id,
                    transaction_type="receipt",
                    quantity=Decimal("0"),
                )
            )
            session.flush()


def test_inventory_transactions_are_immutable_audit_records(session, inventory_setup):
    resource_type, depot, _ = inventory_setup
    transaction = record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="receipt",
        quantity=1,
    )
    transaction.notes = "edited"
    with pytest.raises(ValueError, match="immutable"):
        session.flush()


def test_inventory_transaction_cannot_be_deleted(session, inventory_setup):
    resource_type, depot, _ = inventory_setup
    transaction = record_stock_movement(
        session,
        resource_type_id=resource_type.id,
        depot_id=depot.id,
        transaction_type="receipt",
        quantity=1,
        source="Test fixture",
    )

    session.delete(transaction)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
