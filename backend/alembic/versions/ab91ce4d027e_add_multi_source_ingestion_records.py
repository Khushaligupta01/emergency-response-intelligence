"""add multi-source ingestion logs, HTTP validators and satellite catalogue

Revision ID: ab91ce4d027e
Revises: 6e72a3b0cd41
Create Date: 2026-10-18
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql


revision: str = "ab91ce4d027e"
down_revision: Union[str, Sequence[str], None] = "6e72a3b0cd41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "source_ingestion_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column("request_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_persisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "response_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.CheckConstraint(
            "status IN ('running', 'success', 'partial', 'failed', 'not_modified')",
            name="ck_source_ingestion_runs_status",
        ),
        sa.CheckConstraint(
            "items_seen >= 0 AND items_persisted >= 0 AND items_failed >= 0",
            name="ck_source_ingestion_runs_counts",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_ingestion_runs_source_started",
        "source_ingestion_runs",
        ["source_name", "started_at"],
    )

    op.create_table(
        "source_http_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_name", sa.String(length=80), nullable=False),
        sa.Column("request_url", sa.Text(), nullable=False),
        sa.Column("etag", sa.String(length=500), nullable=True),
        sa.Column("last_modified", sa.String(length=200), nullable=True),
        sa.Column("cache_control", sa.String(length=500), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "validated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_name",
            "request_url",
            name="uq_source_http_state_name_url",
        ),
    )
    op.create_index(
        "ix_source_http_states_source",
        "source_http_states",
        ["source_name"],
    )

    op.create_table(
        "satellite_catalog_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("disaster_sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "disaster_id",
            sa.Integer(),
            sa.ForeignKey("disasters.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("collection_id", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "geometry",
            Geometry(geometry_type="GEOMETRY", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cloud_cover", sa.Numeric(6, 3), nullable=True),
        sa.Column("item_url", sa.Text(), nullable=True),
        sa.Column(
            "assets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(collection_id)) > 0",
            name="ck_satellite_collection_nonempty",
        ),
        sa.CheckConstraint(
            "cloud_cover IS NULL OR (cloud_cover >= 0 AND cloud_cover <= 100)",
            name="ck_satellite_cloud_cover_range",
        ),
        sa.CheckConstraint(
            "ST_SRID(geometry) = 4326 AND ST_IsValid(geometry) AND NOT ST_IsEmpty(geometry)",
            name="ck_satellite_geometry_valid",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id",
            "external_id",
            name="uq_satellite_catalog_source_item",
        ),
    )
    op.create_index(
        "ix_satellite_catalog_disaster_time",
        "satellite_catalog_items",
        ["disaster_id", "acquired_at"],
    )
    op.create_index(
        "ix_satellite_catalog_collection_time",
        "satellite_catalog_items",
        ["collection_id", "acquired_at"],
    )
    op.create_index(
        "ix_satellite_catalog_geometry",
        "satellite_catalog_items",
        ["geometry"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_satellite_catalog_geometry", table_name="satellite_catalog_items")
    op.drop_index(
        "ix_satellite_catalog_collection_time",
        table_name="satellite_catalog_items",
    )
    op.drop_index(
        "ix_satellite_catalog_disaster_time",
        table_name="satellite_catalog_items",
    )
    op.drop_table("satellite_catalog_items")
    op.drop_index("ix_source_http_states_source", table_name="source_http_states")
    op.drop_table("source_http_states")
    op.drop_index(
        "ix_source_ingestion_runs_source_started",
        table_name="source_ingestion_runs",
    )
    op.drop_table("source_ingestion_runs")
