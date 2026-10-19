from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SourceIngestionRun(Base):
    __tablename__ = "source_ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(80), nullable=False)
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    response_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed', 'not_modified')",
            name="ck_source_ingestion_runs_status",
        ),
        CheckConstraint(
            "items_seen >= 0 AND items_persisted >= 0 AND items_failed >= 0",
            name="ck_source_ingestion_runs_counts",
        ),
        Index(
            "ix_source_ingestion_runs_source_started",
            "source_name",
            "started_at",
        ),
    )


class SourceHttpState(Base):
    """Persist validators only; source response bodies are never cached here."""

    __tablename__ = "source_http_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(80), nullable=False)
    request_url: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(String(500))
    last_modified: Mapped[str | None] = mapped_column(String(200))
    cache_control: Mapped[str | None] = mapped_column(String(500))
    response_status: Mapped[int | None] = mapped_column(Integer)
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    __table_args__ = (
        UniqueConstraint("source_name", "request_url", name="uq_source_http_state_name_url"),
        Index("ix_source_http_states_source", "source_name"),
    )


class SatelliteCatalogItem(Base):
    """Metadata-only reference to a Copernicus catalogue item and its remote assets."""

    __tablename__ = "satellite_catalog_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    disaster_id: Mapped[int | None] = mapped_column(
        ForeignKey("disasters.id", ondelete="SET NULL"),
        nullable=True,
    )
    collection_id: Mapped[str] = mapped_column(String(100), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    geometry = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    bbox: Mapped[list] = mapped_column(JSONB, nullable=False)
    cloud_cover: Mapped[Decimal | None] = mapped_column(Numeric(6, 3))
    item_url: Mapped[str | None] = mapped_column(Text)
    assets: Mapped[dict] = mapped_column(JSONB, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_satellite_catalog_source_item"),
        Index("ix_satellite_catalog_disaster_time", "disaster_id", "acquired_at"),
        Index("ix_satellite_catalog_collection_time", "collection_id", "acquired_at"),
        Index(
            "ix_satellite_catalog_geometry",
            "geometry",
            postgresql_using="gist",
        ),
        CheckConstraint("length(trim(collection_id)) > 0", name="ck_satellite_collection_nonempty"),
        CheckConstraint(
            "cloud_cover IS NULL OR (cloud_cover >= 0 AND cloud_cover <= 100)",
            name="ck_satellite_cloud_cover_range",
        ),
        CheckConstraint(
            "ST_SRID(geometry) = 4326 AND ST_IsValid(geometry) AND NOT ST_IsEmpty(geometry)",
            name="ck_satellite_geometry_valid",
        ),
    )
