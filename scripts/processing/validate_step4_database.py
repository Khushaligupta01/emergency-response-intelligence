"""Validate the migrated PostGIS schema and the real Step 1-3 records."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.session import engine


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(relative_path: str) -> dict:
    path = ROOT / relative_path
    require(path.is_file(), f"Required source summary is missing: {relative_path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"Expected JSON object in {relative_path}.")
    return value


def main() -> int:
    buildings_expected = read_json("data/processed/impact/building_impact_summary.json")
    population_expected = read_json("data/processed/impact/population_impact_summary.json")
    zones_expected = read_json("data/processed/zones/zone_severity_summary.json")
    expected_tables = {
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
    }
    expected_spatial_tables = {
        "disasters",
        "hazard_observations",
        "impact_zones",
        "resource_depots",
        "field_operations",
        "building_exposure_footprints",
        "population_datasets",
        "infrastructure_assets",
    }

    with engine.connect() as connection:
        database = connection.execute(text("SELECT current_database()")).scalar_one()
        require(database == "emergency_response", f"Connected to unexpected database: {database}.")
        postgis_version = connection.execute(text("SELECT postgis_full_version()")).scalar_one()
        tables = set(
            connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'spatial_ref_sys'"
                )
            ).scalars()
        )
        require(expected_tables <= tables, f"Missing schema tables: {sorted(expected_tables - tables)}")

        spatial_columns = {
            row.f_table_name: (row.f_geometry_column, row.srid)
            for row in connection.execute(
                text(
                    "SELECT f_table_name, f_geometry_column, srid "
                    "FROM geometry_columns WHERE f_table_schema = 'public'"
                )
            )
        }
        require(
            expected_spatial_tables <= set(spatial_columns),
            f"Missing PostGIS geometry columns: {sorted(expected_spatial_tables - set(spatial_columns))}",
        )
        bad_srids = {
            table: value
            for table, value in spatial_columns.items()
            if table in expected_spatial_tables and value[1] != 4326
        }
        require(not bad_srids, f"Spatial columns have unexpected SRIDs: {bad_srids}")

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
        require(
            expected_spatial_tables <= gist_tables,
            f"Spatial indexes are missing for: {sorted(expected_spatial_tables - gist_tables)}",
        )

        disaster = connection.execute(
            text(
                """
                SELECT id FROM disasters
                WHERE source = 'GDACS' AND external_id = '1104121'
                """
            )
        ).scalar_one()
        source_names = set(
            connection.execute(
                text("SELECT source_name FROM disaster_sources WHERE disaster_id = :id"),
                {"id": disaster},
            ).scalars()
        )
        require({"GDACS", "OSM", "WorldPop"} <= source_names,
                "GDACS, OSM or WorldPop provenance was not saved.")

        hazard = connection.execute(
            text(
                """
                SELECT h.external_id, h.metadata_json,
                       ST_SRID(h.geometry) AS srid,
                       ST_IsValid(h.geometry) AS valid,
                       ST_IsEmpty(h.geometry) AS empty,
                       ST_Area(ST_Transform(h.geometry, 32645)) AS area_m2
                FROM hazard_observations h
                WHERE h.disaster_id = :id
                  AND h.external_id = 'FL-1104121-EP20-Poly_Affected'
                """
            ),
            {"id": disaster},
        ).mappings().one()
        hazard_metadata = json.loads(hazard["metadata_json"])
        require(
            hazard["srid"] == 4326
            and hazard["valid"]
            and not hazard["empty"]
            and hazard["area_m2"] > 0,
            "Stored GDACS hazard geometry failed spatial validation.",
        )
        require(
            hazard_metadata.get("episode_id") == 20
            and hazard_metadata.get("polygon_type") == "Poly_Affected"
            and hazard_metadata.get("not_observed_inundation") is True,
            "Stored GDACS geometry provenance or semantics are incorrect.",
        )

        building_row = connection.execute(
            text(
                """
                SELECT count(*) AS feature_count, sum(footprint_area_km2) AS area_km2,
                       bool_and(ST_SRID(geometry) = 4326
                                AND ST_IsValid(geometry)
                                AND NOT ST_IsEmpty(geometry)) AS geometries_valid
                FROM building_exposure_footprints
                WHERE disaster_id = :id
                """
            ),
            {"id": disaster},
        ).mappings().one()
        require(
            building_row["feature_count"] == buildings_expected["affected_count"],
            "Loaded OSM footprint count does not match the real Step 2D summary.",
        )
        require(building_row["geometries_valid"], "Loaded OSM footprints contain invalid geometry.")
        require(
            abs(float(building_row["area_km2"]) - buildings_expected["affected_footprint_area_km2"])
            <= 1e-6,
            "Loaded OSM footprint area does not match the Step 2D summary.",
        )

        population_row = connection.execute(
            text(
                """
                SELECT d.year, d.crs, d.units, d.valid_pixel_count,
                       e.total_population, e.affected_population,
                       e.total_valid_pixels, e.affected_pixels
                FROM population_datasets d
                JOIN population_exposures e ON e.population_dataset_id = d.id
                WHERE e.disaster_id = :id
                """
            ),
            {"id": disaster},
        ).mappings().one()
        require(
            population_row["year"] == 2020
            and population_row["crs"] == "EPSG:4326"
            and "per pixel" in population_row["units"].lower(),
            "WorldPop year, CRS or units were not correctly registered.",
        )
        require(
            population_row["valid_pixel_count"] == population_expected["total_valid_pixels"]
            and population_row["total_valid_pixels"] == population_expected["total_valid_pixels"]
            and population_row["affected_pixels"] == population_expected["affected_pixels"],
            "Loaded WorldPop pixel counts differ from Step 2D.",
        )
        require(
            abs(float(population_row["affected_population"])
                - population_expected["affected_population"]) <= 1e-5,
            "Loaded WorldPop affected estimate differs from Step 2D.",
        )

        zone_count = connection.execute(
            text("SELECT count(*) FROM impact_zones WHERE disaster_id = :id"),
            {"id": disaster},
        ).scalar_one()
        require(zone_count == zones_expected["cell_count"], "Loaded grid zone count differs from Step 3.")
        spatial_query_result = connection.execute(
            text(
                """
                SELECT count(*)
                FROM impact_zones z
                JOIN hazard_observations h
                  ON h.disaster_id = z.disaster_id
                 AND h.external_id = 'FL-1104121-EP20-Poly_Affected'
                WHERE z.disaster_id = :id
                  AND ST_Intersects(z.geometry, h.geometry)
                """
            ),
            {"id": disaster},
        ).scalar_one()
        require(spatial_query_result == zone_count, "Spatial intersection query did not match all grid zones.")

    print(
        json.dumps(
            {
                "status": "validated",
                "database": database,
                "postgis": postgis_version,
                "event_id": 1104121,
                "episode_id": 20,
                "polygon_type": "Poly_Affected",
                "building_exposure_features": building_row["feature_count"],
                "population_year": population_row["year"],
                "population_valid_pixels": population_row["valid_pixel_count"],
                "grid_zones": zone_count,
                "spatial_query_intersections": spatial_query_result,
                "geometry_columns_epsg4326": sorted(expected_spatial_tables),
                "spatial_indexes": sorted(expected_spatial_tables),
                "infrastructure_records": 0,
                "infrastructure_note": "No approved infrastructure dataset was available to load.",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STEP 4 DATABASE VALIDATION FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise
