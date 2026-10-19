from datetime import datetime
from decimal import Decimal

from geoalchemy2 import Geometry
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ImpactZone(Base):
    __tablename__ = "impact_zones"

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

    zone_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    geometry = mapped_column(
        Geometry(
            geometry_type="GEOMETRY",
            srid=4326,
            spatial_index=False,
        ),
        nullable=False,
    )

    population_count: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 2),
        nullable=True,
    )

    building_count: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    road_length_km: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3),
        nullable=True,
    )

    hazard_coverage: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )

    population_exposure: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 6),
        nullable=True,
    )

    building_exposure: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 6),
        nullable=True,
    )

    infrastructure_exposure: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 6),
        nullable=True,
    )

    building_footprint_area_km2: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 6),
        nullable=True,
    )

    vulnerability_score: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4),
        nullable=True,
    )

    impact_score: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 8),
        nullable=True,
    )

    severity_class: Mapped[str | None] = mapped_column(
        String(30),
        nullable=True,
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

    __table_args__ = (
        Index(
            "ix_impact_zones_geometry",
            "geometry",
            postgresql_using="gist",
        ),
        Index(
            "ix_impact_zones_disaster_id",
            "disaster_id",
        ),
        Index(
            "uq_impact_zones_disaster_code",
            "disaster_id",
            "zone_code",
            unique=True,
        ),
        CheckConstraint(
            "population_count IS NULL OR population_count >= 0",
            name="ck_zone_population_non_negative",
        ),
        CheckConstraint(
            "building_count IS NULL OR building_count >= 0",
            name="ck_zone_buildings_non_negative",
        ),
        CheckConstraint(
            "hazard_coverage IS NULL OR "
            "(hazard_coverage >= 0 AND hazard_coverage <= 1)",
            name="ck_zone_hazard_coverage_range",
        ),
        CheckConstraint(
            "population_exposure IS NULL OR "
            "(population_exposure >= 0 AND population_exposure <= 1)",
            name="ck_zone_population_exposure_range",
        ),
        CheckConstraint(
            "building_exposure IS NULL OR "
            "(building_exposure >= 0 AND building_exposure <= 1)",
            name="ck_zone_building_exposure_range",
        ),
        CheckConstraint(
            "infrastructure_exposure IS NULL OR "
            "(infrastructure_exposure >= 0 AND infrastructure_exposure <= 1)",
            name="ck_zone_infrastructure_exposure_range",
        ),
        CheckConstraint(
            "building_footprint_area_km2 IS NULL OR building_footprint_area_km2 >= 0",
            name="ck_zone_building_footprint_area_nonnegative",
        ),
        CheckConstraint(
            "vulnerability_score IS NULL OR "
            "(vulnerability_score >= 0 AND vulnerability_score <= 1)",
            name="ck_zone_vulnerability_range",
        ),
        CheckConstraint(
            "impact_score IS NULL OR "
            "(impact_score >= 0 AND impact_score <= 1)",
            name="ck_zone_impact_range",
        ),
    )