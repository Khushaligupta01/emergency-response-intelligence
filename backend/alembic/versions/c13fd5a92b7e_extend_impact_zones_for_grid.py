"""extend impact zones for projected grid exposure metrics

Revision ID: c13fd5a92b7e
Revises: b65de4c61e29
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry


revision: str = "c13fd5a92b7e"
down_revision: Union[str, Sequence[str], None] = "b65de4c61e29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "impact_zones",
        "geometry",
        existing_type=Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        type_=Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        postgresql_using="geometry::geometry(Geometry,4326)",
    )
    op.alter_column(
        "impact_zones",
        "population_count",
        existing_type=sa.Integer(),
        type_=sa.Numeric(18, 2),
        postgresql_using="population_count::numeric(18,2)",
    )
    op.add_column(
        "impact_zones",
        sa.Column("population_exposure", sa.Numeric(7, 6), nullable=True),
    )
    op.add_column(
        "impact_zones",
        sa.Column("building_exposure", sa.Numeric(7, 6), nullable=True),
    )
    op.add_column(
        "impact_zones",
        sa.Column("infrastructure_exposure", sa.Numeric(7, 6), nullable=True),
    )
    op.add_column(
        "impact_zones",
        sa.Column("building_footprint_area_km2", sa.Numeric(14, 6), nullable=True),
    )
    op.create_check_constraint(
        "ck_zone_population_exposure_range",
        "impact_zones",
        "population_exposure IS NULL OR (population_exposure >= 0 AND population_exposure <= 1)",
    )
    op.create_check_constraint(
        "ck_zone_building_exposure_range",
        "impact_zones",
        "building_exposure IS NULL OR (building_exposure >= 0 AND building_exposure <= 1)",
    )
    op.create_check_constraint(
        "ck_zone_infrastructure_exposure_range",
        "impact_zones",
        "infrastructure_exposure IS NULL OR "
        "(infrastructure_exposure >= 0 AND infrastructure_exposure <= 1)",
    )
    op.create_check_constraint(
        "ck_zone_building_footprint_area_nonnegative",
        "impact_zones",
        "building_footprint_area_km2 IS NULL OR building_footprint_area_km2 >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_zone_building_footprint_area_nonnegative",
        "impact_zones",
        type_="check",
    )
    op.drop_constraint(
        "ck_zone_infrastructure_exposure_range",
        "impact_zones",
        type_="check",
    )
    op.drop_constraint(
        "ck_zone_building_exposure_range",
        "impact_zones",
        type_="check",
    )
    op.drop_constraint(
        "ck_zone_population_exposure_range",
        "impact_zones",
        type_="check",
    )
    op.drop_column("impact_zones", "building_footprint_area_km2")
    op.drop_column("impact_zones", "infrastructure_exposure")
    op.drop_column("impact_zones", "building_exposure")
    op.drop_column("impact_zones", "population_exposure")
    op.alter_column(
        "impact_zones",
        "population_count",
        existing_type=sa.Numeric(18, 2),
        type_=sa.Integer(),
        postgresql_using="round(population_count)::integer",
    )
    op.alter_column(
        "impact_zones",
        "geometry",
        existing_type=Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
        type_=Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
        postgresql_using="geometry::geometry(Polygon,4326)",
    )
