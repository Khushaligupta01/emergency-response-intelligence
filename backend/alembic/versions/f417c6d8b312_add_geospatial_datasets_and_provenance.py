"""add geospatial datasets and provenance-backed impact records

Revision ID: f417c6d8b312
Revises: c13fd5a92b7e
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


revision: str = "f417c6d8b312"
down_revision: Union[str, Sequence[str], None] = "c13fd5a92b7e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "impact_zones",
        "impact_score",
        existing_type=sa.Numeric(5, 4),
        type_=sa.Numeric(10, 8),
        existing_nullable=True,
        postgresql_using="impact_score::numeric(10,8)",
    )

    op.add_column(
        "hazard_observations",
        sa.Column("external_id", sa.String(length=200), nullable=True),
    )
    op.create_index(
        "uq_hazard_observations_source_external_id",
        "hazard_observations",
        ["source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.create_table(
        "building_exposure_footprints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey("disasters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_feature_id", sa.String(length=100), nullable=False),
        sa.Column(
            "geometry",
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("footprint_area_km2", sa.Numeric(16, 10), nullable=False),
        sa.Column("source_properties", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "footprint_area_km2 > 0",
            name="ck_building_exposure_area_positive",
        ),
        sa.CheckConstraint(
            "ST_SRID(geometry) = 4326 AND NOT ST_IsEmpty(geometry) "
            "AND ST_IsValid(geometry) "
            "AND GeometryType(geometry) IN ('POLYGON', 'MULTIPOLYGON')",
            name="ck_building_exposure_geometry_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "disaster_id",
            "source_id",
            "source_feature_id",
            name="uq_building_exposure_disaster_source_feature",
        ),
    )
    op.create_index(
        "ix_building_exposure_disaster_id",
        "building_exposure_footprints",
        ["disaster_id"],
    )
    op.create_index(
        "ix_building_exposure_geometry",
        "building_exposure_footprints",
        ["geometry"],
        postgresql_using="gist",
    )

    op.create_table(
        "population_datasets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column("dataset_name", sa.String(length=255), nullable=False),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("license", sa.String(length=255), nullable=True),
        sa.Column("resolution_x", sa.Numeric(16, 12), nullable=False),
        sa.Column("resolution_y", sa.Numeric(16, 12), nullable=False),
        sa.Column("crs", sa.String(length=32), nullable=False),
        sa.Column("units", sa.String(length=120), nullable=False),
        sa.Column("value_semantics", sa.Text(), nullable=False),
        sa.Column("nodata_value", sa.Numeric(20, 8), nullable=True),
        sa.Column("pixel_dtype", sa.String(length=32), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("valid_pixel_count", sa.BigInteger(), nullable=False),
        sa.Column("minimum_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("maximum_value", sa.Numeric(20, 8), nullable=False),
        sa.Column("raster_path", sa.Text(), nullable=False),
        sa.Column("bounds", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "coverage_geometry",
            Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("year > 1900", name="ck_population_dataset_year"),
        sa.CheckConstraint(
            "resolution_x > 0 AND resolution_y > 0",
            name="ck_population_dataset_resolution_positive",
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0 AND valid_pixel_count >= 0",
            name="ck_population_dataset_dimensions",
        ),
        sa.CheckConstraint(
            "minimum_value >= 0 AND maximum_value >= minimum_value",
            name="ck_population_dataset_value_range",
        ),
        sa.CheckConstraint(
            "ST_SRID(coverage_geometry) = 4326 AND NOT ST_IsEmpty(coverage_geometry) "
            "AND ST_IsValid(coverage_geometry)",
            name="ck_population_dataset_coverage_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_name",
            "dataset_name",
            "year",
            name="uq_population_dataset_source_name_year",
        ),
    )
    op.create_index(
        "ix_population_datasets_source_id",
        "population_datasets",
        ["source_id"],
    )
    op.create_index(
        "ix_population_datasets_coverage_geometry",
        "population_datasets",
        ["coverage_geometry"],
        postgresql_using="gist",
    )

    op.create_table(
        "population_exposures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "population_dataset_id",
            sa.Integer(),
            sa.ForeignKey("population_datasets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey("disasters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "hazard_observation_id",
            sa.Integer(),
            sa.ForeignKey("hazard_observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("total_population", sa.Numeric(20, 6), nullable=False),
        sa.Column("affected_population", sa.Numeric(20, 6), nullable=False),
        sa.Column("total_valid_pixels", sa.BigInteger(), nullable=False),
        sa.Column("affected_pixels", sa.BigInteger(), nullable=False),
        sa.Column("analysis_method", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "total_population >= 0 AND affected_population >= 0 "
            "AND affected_population <= total_population",
            name="ck_population_exposure_population_range",
        ),
        sa.CheckConstraint(
            "total_valid_pixels >= 0 AND affected_pixels >= 0 "
            "AND affected_pixels <= total_valid_pixels",
            name="ck_population_exposure_pixel_range",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "population_dataset_id",
            "disaster_id",
            "hazard_observation_id",
            name="uq_population_exposure_dataset_disaster_hazard",
        ),
    )
    op.create_index(
        "ix_population_exposures_disaster_id",
        "population_exposures",
        ["disaster_id"],
    )

    op.create_table(
        "infrastructure_assets",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=150), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("asset_type", sa.String(length=80), nullable=False),
        sa.Column(
            "geometry",
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("administrative_area", sa.String(length=150), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(asset_type)) > 0",
            name="ck_infrastructure_asset_type",
        ),
        sa.CheckConstraint(
            "ST_SRID(geometry) = 4326 AND NOT ST_IsEmpty(geometry) "
            "AND ST_IsValid(geometry)",
            name="ck_infrastructure_asset_geometry_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "external_id",
            name="uq_infrastructure_assets_source_external_id",
        ),
    )
    op.create_index(
        "ix_infrastructure_assets_source_id",
        "infrastructure_assets",
        ["source_id"],
    )
    op.create_index(
        "ix_infrastructure_assets_geometry",
        "infrastructure_assets",
        ["geometry"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_infrastructure_assets_geometry", table_name="infrastructure_assets")
    op.drop_index("ix_infrastructure_assets_source_id", table_name="infrastructure_assets")
    op.drop_table("infrastructure_assets")
    op.drop_index("ix_population_exposures_disaster_id", table_name="population_exposures")
    op.drop_table("population_exposures")
    op.drop_index(
        "ix_population_datasets_coverage_geometry",
        table_name="population_datasets",
    )
    op.drop_index("ix_population_datasets_source_id", table_name="population_datasets")
    op.drop_table("population_datasets")
    op.drop_index(
        "ix_building_exposure_geometry",
        table_name="building_exposure_footprints",
    )
    op.drop_index("ix_building_exposure_disaster_id", table_name="building_exposure_footprints")
    op.drop_table("building_exposure_footprints")
    op.drop_index(
        "uq_hazard_observations_source_external_id",
        table_name="hazard_observations",
    )
    op.drop_column("hazard_observations", "external_id")
    op.alter_column(
        "impact_zones",
        "impact_score",
        existing_type=sa.Numeric(10, 8),
        type_=sa.Numeric(5, 4),
        existing_nullable=True,
        postgresql_using="impact_score::numeric(5,4)",
    )
