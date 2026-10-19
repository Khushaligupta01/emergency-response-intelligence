from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FieldOperation(Base):
    __tablename__ = "field_operations"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    disaster_id: Mapped[int] = mapped_column(
        ForeignKey(
            "disasters.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    zone_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "impact_zones.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    team_name: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    operation_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="planned",
    )

    location = mapped_column(
        Geometry(
            geometry_type="POINT",
            srid=4326,
            spatial_index=False,
        ),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_field_operations_location",
            "location",
            postgresql_using="gist",
        ),
        Index(
            "ix_field_operations_disaster_id",
            "disaster_id",
        ),
    )