import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import engine


@pytest.fixture()
def connection():
    with engine.connect() as db:
        transaction = db.begin()
        try:
            yield db
        finally:
            transaction.rollback()


def test_postgresql_postgis_connection_and_required_tables(connection):
    assert connection.execute(text("SELECT current_database()")).scalar_one() == "emergency_response"
    assert connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis')")
    ).scalar_one()

    tables = set(
        connection.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'spatial_ref_sys'"
            )
        ).scalars()
    )
    assert {
        "disasters",
        "disaster_sources",
        "hazard_observations",
        "impact_zones",
        "population_datasets",
        "population_exposures",
        "infrastructure_assets",
        "resource_types",
        "resource_depots",
        "inventory_transactions",
        "inventory_balances",
        "field_operations",
        "building_exposure_footprints",
        "source_ingestion_runs",
        "source_http_states",
        "satellite_catalog_items",
    } <= tables


def test_spatial_columns_have_expected_srid_and_gist_indexes(connection):
    spatial_tables = {
        row.f_table_name: (row.f_geometry_column, row.srid)
        for row in connection.execute(
            text(
                """
                SELECT f_table_name, f_geometry_column, srid
                FROM geometry_columns
                WHERE f_table_schema = 'public'
                """
            )
        )
    }
    assert spatial_tables == {
        "disasters": ("geometry", 4326),
        "field_operations": ("location", 4326),
        "hazard_observations": ("geometry", 4326),
        "impact_zones": ("geometry", 4326),
        "resource_depots": ("geometry", 4326),
        "building_exposure_footprints": ("geometry", 4326),
        "population_datasets": ("coverage_geometry", 4326),
        "infrastructure_assets": ("geometry", 4326),
        "satellite_catalog_items": ("geometry", 4326),
    }

    gist_tables = set(
        connection.execute(
            text(
                """
                SELECT DISTINCT t.relname
                FROM pg_index x
                JOIN pg_class t ON t.oid = x.indrelid
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_am am ON am.oid = i.relam
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = 'public' AND am.amname = 'gist'
                """
            )
        ).scalars()
    )
    assert {
        "disasters",
        "field_operations",
        "hazard_observations",
        "impact_zones",
        "resource_depots",
        "building_exposure_footprints",
        "population_datasets",
        "infrastructure_assets",
        "satellite_catalog_items",
    } <= gist_tables


def test_postgis_spatial_queries_and_loaded_gdacs_geometry(connection):
    row = connection.execute(
        text(
            """
            SELECT ST_SRID(h.geometry) AS srid,
                   ST_IsValid(h.geometry) AS valid,
                   ST_IsEmpty(h.geometry) AS empty,
                   ST_GeometryType(h.geometry) AS geometry_type,
                   ST_Area(ST_Transform(h.geometry, 32645)) AS area_m2,
                   ST_Intersects(h.geometry, ST_SetSRID(ST_Point(85.0, 25.0), 4326))
                       AS point_intersects
            FROM hazard_observations h
            JOIN disasters d ON d.id = h.disaster_id
            WHERE d.external_id = '1104121'
              AND h.external_id = 'FL-1104121-EP20-Poly_Affected'
            """
        )
    ).mappings().one()
    assert row["srid"] == 4326
    assert row["valid"] and not row["empty"]
    assert row["geometry_type"] == "ST_MultiPolygon"
    assert row["area_m2"] > 0
    assert isinstance(row["point_intersects"], bool)


def test_real_step1_to_step3_datasets_are_linked_and_loaded(connection):
    sources = set(
        connection.execute(
            text("SELECT source_name FROM disaster_sources WHERE disaster_id = 1")
        ).scalars()
    )
    assert {"GDACS", "OSM", "WorldPop"} <= sources

    buildings = connection.execute(
        text(
            """
            SELECT count(*) AS count, sum(footprint_area_km2) AS area_km2
            FROM building_exposure_footprints
            WHERE disaster_id = 1
            """
        )
    ).mappings().one()
    assert buildings["count"] == 198182
    assert math.isclose(float(buildings["area_km2"]), 20.4370001175, abs_tol=1e-7)

    population = connection.execute(
        text(
            """
            SELECT d.year, d.crs, d.units, d.valid_pixel_count,
                   e.total_valid_pixels, e.affected_pixels,
                   e.total_population, e.affected_population
            FROM population_datasets d
            JOIN population_exposures e ON e.population_dataset_id = d.id
            WHERE e.disaster_id = 1
            """
        )
    ).mappings().one()
    assert population["year"] == 2020
    assert population["crs"] == "EPSG:4326"
    assert "per pixel" in population["units"]
    assert population["valid_pixel_count"] == population["total_valid_pixels"] == 4730704
    assert population["affected_pixels"] == 4721961
    assert math.isclose(float(population["affected_population"]), 127789039.647878, abs_tol=1e-5)

    assert connection.execute(
        text("SELECT count(*) FROM impact_zones WHERE disaster_id = 1")
    ).scalar_one() == 1072
    assert connection.execute(text("SELECT count(*) FROM infrastructure_assets")).scalar_one() == 0


def test_foreign_keys_reject_invalid_source_reference(connection):
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(
                text(
                    """
                    INSERT INTO infrastructure_assets
                        (source_id, external_id, asset_type, geometry)
                    VALUES
                        (2147483647, 'invalid-source-test', 'test',
                         ST_SetSRID(ST_Point(85, 25), 4326))
                    """
                )
            )


def test_check_constraints_reject_invalid_population_impact(connection):
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(
                text(
                    """
                    INSERT INTO population_exposures
                        (population_dataset_id, disaster_id, hazard_observation_id,
                         total_population, affected_population, total_valid_pixels,
                         affected_pixels, analysis_method)
                    VALUES
                        (1, 1, 1, 10, 11, 10, 5, 'constraint test')
                    """
                )
            )
