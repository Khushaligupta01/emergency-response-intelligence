from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DisasterSource(Base):
    __tablename__ = "disaster_sources"

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

    source_name: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    external_id: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    source_url: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    raw_data_path: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    payload_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    metadata_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (
        Index(
            "ix_disaster_sources_disaster_id",
            "disaster_id",
        ),
        Index(
            "ix_disaster_sources_source_external_id",
            "source_name",
            "external_id",
            unique=True,
        ),
    )


class HazardObservation(Base):
    __tablename__ = "hazard_observations"

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

    source_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "disaster_sources.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    hazard_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    external_id: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )

    observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    geometry = mapped_column(
        Geometry(
            geometry_type="GEOMETRY",
            srid=4326,
            spatial_index=False,
        ),
        nullable=True,
    )

    raster_path: Mapped[str | None] = mapped_column(
        String(1000),
        nullable=True,
    )

    metadata_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_hazard_observations_geometry",
            "geometry",
            postgresql_using="gist",
        ),
        Index(
            "ix_hazard_observations_disaster_id",
            "disaster_id",
        ),
        Index(
            "uq_hazard_observations_source_external_id",
            "source_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
    )