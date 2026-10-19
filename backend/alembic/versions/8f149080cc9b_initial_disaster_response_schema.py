"""initial disaster response schema

Revision ID: 8f149080cc9b
Revises:
Create Date: 2026-10-15

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry


# revision identifiers, used by Alembic.
revision: str = "8f149080cc9b"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---------------------------------------------------------
    # 1. Enable PostGIS
    # ---------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    # ---------------------------------------------------------
    # 2. DISASTERS
    # ---------------------------------------------------------
    op.create_table(
        "disasters",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("external_id", sa.String(length=100), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("disaster_type", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("country", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=True),
        sa.Column("start_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "geometry",
            Geometry(
                geometry_type="GEOMETRY",
                srid=4326,
                spatial_index=False,
            ),
            nullable=True,
        ),
        sa.Column("metadata_json", sa.Text(), nullable=True),
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
            "start_time IS NULL OR end_time IS NULL OR end_time >= start_time",
            name="ck_disasters_valid_time_range",
        ),
    )

    op.create_index(
        "ix_disasters_source_external_id",
        "disasters",
        ["source", "external_id"],
        unique=True,
    )

    op.create_index(
        "ix_disasters_disaster_type",
        "disasters",
        ["disaster_type"],
    )

    op.create_index(
        "ix_disasters_geometry",
        "disasters",
        ["geometry"],
        postgresql_using="gist",
    )

    # ---------------------------------------------------------
    # 3. RESOURCE DEPOTS
    # ---------------------------------------------------------
    op.create_table(
        "resource_depots",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column(
            "geometry",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
            ),
            nullable=False,
        ),
        sa.Column("contact_name", sa.String(length=100), nullable=True),
        sa.Column("contact_phone", sa.String(length=30), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_resource_depots_geometry",
        "resource_depots",
        ["geometry"],
        postgresql_using="gist",
    )

    # ---------------------------------------------------------
    # 4. RESOURCE TYPES
    # ---------------------------------------------------------
    op.create_table(
        "resource_types",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("unit", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
    )

    op.create_index(
        "uq_resource_types_code",
        "resource_types",
        ["code"],
        unique=True,
    )

    # ---------------------------------------------------------
    # 5. DISASTER SOURCES
    # ---------------------------------------------------------
    op.create_table(
        "disaster_sources",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),

        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey(
                "disasters.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column("source_name", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=150), nullable=False),
        sa.Column("source_url", sa.String(length=1000), nullable=True),
        sa.Column("raw_data_path", sa.String(length=1000), nullable=True),

        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),

        sa.Column("payload_hash", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_disaster_sources_disaster_id",
        "disaster_sources",
        ["disaster_id"],
    )

    op.create_index(
        "ix_disaster_sources_source_external_id",
        "disaster_sources",
        ["source_name", "external_id"],
        unique=True,
    )

    # ---------------------------------------------------------
    # 6. IMPACT ZONES
    # ---------------------------------------------------------
    op.create_table(
        "impact_zones",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),

        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey(
                "disasters.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column("zone_code", sa.String(length=100), nullable=False),

        sa.Column(
            "geometry",
            Geometry(
                geometry_type="POLYGON",
                srid=4326,
                spatial_index=False,
            ),
            nullable=False,
        ),

        sa.Column("population_count", sa.Integer(), nullable=True),
        sa.Column("building_count", sa.Integer(), nullable=True),
        sa.Column(
            "road_length_km",
            sa.Numeric(12, 3),
            nullable=True,
        ),

        sa.Column(
            "hazard_coverage",
            sa.Numeric(5, 4),
            nullable=True,
        ),

        sa.Column(
            "vulnerability_score",
            sa.Numeric(5, 4),
            nullable=True,
        ),

        sa.Column(
            "impact_score",
            sa.Numeric(5, 4),
            nullable=True,
        ),

        sa.Column(
            "severity_class",
            sa.String(length=30),
            nullable=True,
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
            "population_count IS NULL OR population_count >= 0",
            name="ck_impact_zones_population_nonnegative",
        ),

        sa.CheckConstraint(
            "building_count IS NULL OR building_count >= 0",
            name="ck_impact_zones_buildings_nonnegative",
        ),

        sa.CheckConstraint(
            "hazard_coverage IS NULL OR "
            "(hazard_coverage >= 0 AND hazard_coverage <= 1)",
            name="ck_impact_zones_hazard_coverage",
        ),

        sa.CheckConstraint(
            "vulnerability_score IS NULL OR "
            "(vulnerability_score >= 0 AND vulnerability_score <= 1)",
            name="ck_impact_zones_vulnerability_score",
        ),

        sa.CheckConstraint(
            "impact_score IS NULL OR "
            "(impact_score >= 0 AND impact_score <= 1)",
            name="ck_impact_zones_impact_score",
        ),
    )

    op.create_index(
        "ix_impact_zones_disaster_id",
        "impact_zones",
        ["disaster_id"],
    )

    op.create_index(
        "ix_impact_zones_geometry",
        "impact_zones",
        ["geometry"],
        postgresql_using="gist",
    )

    op.create_index(
        "uq_impact_zones_disaster_code",
        "impact_zones",
        ["disaster_id", "zone_code"],
        unique=True,
    )

    # ---------------------------------------------------------
    # 7. INVENTORY
    # ---------------------------------------------------------
    op.create_table(
        "inventory",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),

        sa.Column(
            "depot_id",
            sa.Integer(),
            sa.ForeignKey(
                "resource_depots.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column(
            "resource_type_id",
            sa.Integer(),
            sa.ForeignKey(
                "resource_types.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column(
            "quantity",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),

        sa.Column(
            "reserved_quantity",
            sa.Numeric(14, 2),
            nullable=False,
            server_default=sa.text("0"),
        ),

        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),

        sa.CheckConstraint(
            "quantity >= 0",
            name="ck_inventory_quantity_nonnegative",
        ),

        sa.CheckConstraint(
            "reserved_quantity >= 0",
            name="ck_inventory_reserved_nonnegative",
        ),

        sa.CheckConstraint(
            "reserved_quantity <= quantity",
            name="ck_inventory_reserved_lte_quantity",
        ),
    )

    op.create_index(
        "uq_inventory_depot_resource",
        "inventory",
        ["depot_id", "resource_type_id"],
        unique=True,
    )

    # ---------------------------------------------------------
    # 8. FIELD OPERATIONS
    # ---------------------------------------------------------
    op.create_table(
        "field_operations",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),

        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey(
                "disasters.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column(
            "zone_id",
            sa.Integer(),
            sa.ForeignKey(
                "impact_zones.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),

        sa.Column("team_name", sa.String(length=150), nullable=False),
        sa.Column("operation_type", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),

        sa.Column(
            "location",
            Geometry(
                geometry_type="POINT",
                srid=4326,
                spatial_index=False,
            ),
            nullable=True,
        ),

        sa.Column("notes", sa.String(length=1000), nullable=True),

        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_field_operations_disaster_id",
        "field_operations",
        ["disaster_id"],
    )

    op.create_index(
        "ix_field_operations_location",
        "field_operations",
        ["location"],
        postgresql_using="gist",
    )

    # ---------------------------------------------------------
    # 9. HAZARD OBSERVATIONS
    # ---------------------------------------------------------
    op.create_table(
        "hazard_observations",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),

        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey(
                "disasters.id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),

        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey(
                "disaster_sources.id",
                ondelete="CASCADE",
            ),
            nullable=True,
        ),

        sa.Column("hazard_type", sa.String(length=50), nullable=False),

        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "geometry",
            Geometry(
                geometry_type="GEOMETRY",
                srid=4326,
                spatial_index=False,
            ),
            nullable=True,
        ),

        sa.Column(
            "raster_path",
            sa.String(length=1000),
            nullable=True,
        ),

        sa.Column(
            "metadata_json",
            sa.Text(),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_index(
        "ix_hazard_observations_disaster_id",
        "hazard_observations",
        ["disaster_id"],
    )

    op.create_index(
        "ix_hazard_observations_geometry",
        "hazard_observations",
        ["geometry"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    # Drop application tables only.
    # DO NOT drop spatial_ref_sys because it belongs to PostGIS.

    op.drop_index(
        "ix_hazard_observations_geometry",
        table_name="hazard_observations",
    )

    op.drop_index(
        "ix_hazard_observations_disaster_id",
        table_name="hazard_observations",
    )

    op.drop_table("hazard_observations")

    op.drop_index(
        "ix_field_operations_location",
        table_name="field_operations",
    )

    op.drop_index(
        "ix_field_operations_disaster_id",
        table_name="field_operations",
    )

    op.drop_table("field_operations")

    op.drop_index(
        "uq_inventory_depot_resource",
        table_name="inventory",
    )

    op.drop_table("inventory")

    op.drop_index(
        "uq_impact_zones_disaster_code",
        table_name="impact_zones",
    )

    op.drop_index(
        "ix_impact_zones_geometry",
        table_name="impact_zones",
    )

    op.drop_index(
        "ix_impact_zones_disaster_id",
        table_name="impact_zones",
    )

    op.drop_table("impact_zones")

    op.drop_index(
        "ix_disaster_sources_source_external_id",
        table_name="disaster_sources",
    )

    op.drop_index(
        "ix_disaster_sources_disaster_id",
        table_name="disaster_sources",
    )

    op.drop_table("disaster_sources")

    op.drop_index(
        "uq_resource_types_code",
        table_name="resource_types",
    )

    op.drop_table("resource_types")

    op.drop_index(
        "ix_resource_depots_geometry",
        table_name="resource_depots",
    )

    op.drop_table("resource_depots")

    op.drop_index(
        "ix_disasters_geometry",
        table_name="disasters",
    )

    op.drop_index(
        "ix_disasters_disaster_type",
        table_name="disasters",
    )

    op.drop_index(
        "ix_disasters_source_external_id",
        table_name="disasters",
    )

    op.drop_table("disasters")