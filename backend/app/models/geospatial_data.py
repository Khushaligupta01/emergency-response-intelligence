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


class BuildingExposureFootprint(Base):
    """OSM footprint portions within one GDACS reported affected area."""

    __tablename__ = "building_exposure_footprints"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    disaster_id: Mapped[int] = mapped_column(
        ForeignKey("disasters.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_feature_id: Mapped[str] = mapped_column(String(100), nullable=False)
    geometry = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    footprint_area_km2: Mapped[Decimal] = mapped_column(Numeric(16, 10), nullable=False)
    source_properties: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "disaster_id",
            "source_id",
            "source_feature_id",
            name="uq_building_exposure_disaster_source_feature",
        ),
        Index("ix_building_exposure_disaster_id", "disaster_id"),
        Index(
            "ix_building_exposure_geometry",
            "geometry",
            postgresql_using="gist",
        ),
        CheckConstraint(
            "footprint_area_km2 > 0",
            name="ck_building_exposure_area_positive",
        ),
        CheckConstraint(
            "ST_SRID(geometry) = 4326 AND NOT ST_IsEmpty(geometry) "
            "AND ST_IsValid(geometry) "
            "AND GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')",
            name="ck_building_exposure_geometry_valid",
        ),
    )


class PopulationDataset(Base):
    """Provenance and spatial metadata for a population raster, not its pixels."""

    __tablename__ = "population_datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_name: Mapped[str] = mapped_column(String(80), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(255), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    license: Mapped[str | None] = mapped_column(String(255))
    resolution_x: Mapped[Decimal] = mapped_column(Numeric(16, 12), nullable=False)
    resolution_y: Mapped[Decimal] = mapped_column(Numeric(16, 12), nullable=False)
    crs: Mapped[str] = mapped_column(String(32), nullable=False)
    units: Mapped[str] = mapped_column(String(120), nullable=False)
    value_semantics: Mapped[str] = mapped_column(Text, nullable=False)
    nodata_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    pixel_dtype: Mapped[str] = mapped_column(String(32), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_pixel_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    minimum_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    maximum_value: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    raster_path: Mapped[str] = mapped_column(Text, nullable=False)
    bounds: Mapped[dict] = mapped_column(JSONB, nullable=False)
    coverage_geometry = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=False,
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
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

    __table_args__ = (
        UniqueConstraint(
            "source_name",
            "dataset_name",
            "year",
            name="uq_population_dataset_source_name_year",
        ),
        Index("ix_population_datasets_source_id", "source_id"),
        Index(
            "ix_population_datasets_coverage_geometry",
            "coverage_geometry",
            postgresql_using="gist",
        ),
        CheckConstraint("year > 1900", name="ck_population_dataset_year"),
        CheckConstraint(
            "resolution_x > 0 AND resolution_y > 0",
            name="ck_population_dataset_resolution_positive",
        ),
        CheckConstraint(
            "width > 0 AND height > 0 AND valid_pixel_count >= 0",
            name="ck_population_dataset_dimensions",
        ),
        CheckConstraint(
            "minimum_value >= 0 AND maximum_value >= minimum_value",
            name="ck_population_dataset_value_range",
        ),
        CheckConstraint(
            "ST_SRID(coverage_geometry) = 4326 AND NOT ST_IsEmpty(coverage_geometry) "
            "AND ST_IsValid(coverage_geometry)",
            name="ck_population_dataset_coverage_valid",
        ),
    )


class PopulationExposure(Base):
    """Population-impact totals derived from a documented raster and hazard geometry."""

    __tablename__ = "population_exposures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    population_dataset_id: Mapped[int] = mapped_column(
        ForeignKey("population_datasets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    disaster_id: Mapped[int] = mapped_column(
        ForeignKey("disasters.id", ondelete="CASCADE"),
        nullable=False,
    )
    hazard_observation_id: Mapped[int] = mapped_column(
        ForeignKey("hazard_observations.id", ondelete="CASCADE"),
        nullable=False,
    )
    total_population: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    affected_population: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    total_valid_pixels: Mapped[int] = mapped_column(BigInteger, nullable=False)
    affected_pixels: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_method: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "population_dataset_id",
            "disaster_id",
            "hazard_observation_id",
            name="uq_population_exposure_dataset_disaster_hazard",
        ),
        Index("ix_population_exposures_disaster_id", "disaster_id"),
        CheckConstraint(
            "total_population >= 0 AND affected_population >= 0 "
            "AND affected_population <= total_population",
            name="ck_population_exposure_population_range",
        ),
        CheckConstraint(
            "total_valid_pixels >= 0 AND affected_pixels >= 0 "
            "AND affected_pixels <= total_valid_pixels",
            name="ck_population_exposure_pixel_range",
        ),
    )


class InfrastructureAsset(Base):
    """Source-attributed infrastructure inventory; no records are seeded by Step 4."""

    __tablename__ = "infrastructure_assets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(String(150), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    asset_type: Mapped[str] = mapped_column(String(80), nullable=False)
    geometry = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        nullable=False,
    )
    administrative_area: Mapped[str | None] = mapped_column(String(150))
    is_active: Mapped[bool] = mapped_column(
        default=True,
        server_default="true",
        nullable=False,
    )
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
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

    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "external_id",
            name="uq_infrastructure_assets_source_external_id",
        ),
        Index("ix_infrastructure_assets_source_id", "source_id"),
        Index(
            "ix_infrastructure_assets_geometry",
            "geometry",
            postgresql_using="gist",
        ),
        CheckConstraint("length(trim(asset_type)) > 0", name="ck_infrastructure_asset_type"),
        CheckConstraint(
            "ST_SRID(geometry) = 4326 AND NOT ST_IsEmpty(geometry) "
            "AND ST_IsValid(geometry)",
            name="ck_infrastructure_asset_geometry_valid",
        ),
    )
