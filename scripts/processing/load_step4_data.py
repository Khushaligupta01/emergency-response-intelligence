"""Load validated Step 1-3 source data and impact summaries into PostGIS."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import pyarrow as pa
import rasterio
import shapely
from pyogrio.raw import open_arrow
from pyproj import CRS
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.db.session import engine


FLOOD_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent.geojson"
FLOOD_METADATA_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent_metadata.json"
BUILDINGS_PATH = ROOT / "data/processed/impact/affected_buildings.geojson"
BUILDING_METADATA_PATH = ROOT / "data/processed/osm/bihar_buildings_metadata.json"
BUILDING_SUMMARY_PATH = ROOT / "data/processed/impact/building_impact_summary.json"
POPULATION_PATH = ROOT / "data/processed/population/bihar_population.tif"
POPULATION_METADATA_PATH = ROOT / "data/processed/population/bihar_population_metadata.json"
POPULATION_SUMMARY_PATH = ROOT / "data/processed/impact/population_impact_summary.json"
IMPACT_SUMMARY_PATH = ROOT / "data/processed/impact/impact_assessment_summary.json"
GRID_PATH = ROOT / "data/processed/zones/disaster_grid.geojson"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    require(path.is_file(), f"Required source file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to read {path}: {error}") from error
    require(isinstance(value, dict), f"Expected a JSON object in {path}.")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_id(
    connection,
    *,
    disaster_id: int,
    source_name: str,
    external_id: str,
    source_url: str | None,
    raw_data_path: str,
    payload_hash: str,
    metadata: dict,
) -> int:
    result = connection.execute(
        text(
            """
            INSERT INTO disaster_sources
                (disaster_id, source_name, external_id, source_url, raw_data_path,
                 payload_hash, metadata_json)
            VALUES
                (:disaster_id, :source_name, :external_id, :source_url, :raw_data_path,
                 :payload_hash, :metadata_json)
            ON CONFLICT (source_name, external_id) DO UPDATE SET
                disaster_id = EXCLUDED.disaster_id,
                source_url = EXCLUDED.source_url,
                raw_data_path = EXCLUDED.raw_data_path,
                payload_hash = EXCLUDED.payload_hash,
                metadata_json = EXCLUDED.metadata_json
            RETURNING id
            """
        ),
        {
            "disaster_id": disaster_id,
            "source_name": source_name,
            "external_id": external_id,
            "source_url": source_url,
            "raw_data_path": raw_data_path,
            "payload_hash": payload_hash,
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
    )
    return int(result.scalar_one())


def validate_sources(
    flood: dict,
    flood_metadata: dict,
    building_summary: dict,
    osm_metadata: dict,
    population_summary: dict,
    population_metadata: dict,
    impact_summary: dict,
    grid: dict,
) -> None:
    require(flood.get("type") == "FeatureCollection", "GDACS input must be a FeatureCollection.")
    flood_features = flood.get("features")
    require(isinstance(flood_features, list) and flood_features, "GDACS affected geometry is empty.")
    require(flood.get("coordinate_reference_system") == "EPSG:4326", "GDACS CRS must be EPSG:4326.")
    properties = flood_features[0].get("properties", {})
    require(properties.get("event_id") == 1104121, "Unexpected GDACS event ID.")
    require(properties.get("episode_id") == 20, "Expected GDACS episode 20.")
    require(properties.get("polygon_type") == "Poly_Affected", "Expected GDACS Poly_Affected.")
    require(flood_metadata.get("not_observed_inundation") is True,
            "GDACS metadata must not characterize this as observed inundation.")
    require(building_summary.get("event_id") == 1104121
            and building_summary.get("affected_count", 0) > 0,
            "OSM impact summary is missing or does not match the GDACS event.")
    require(osm_metadata.get("synthetic_generation_used") is False,
            "OSM metadata must explicitly state that synthetic generation was not used.")
    require(population_summary.get("event_id") == 1104121
            and population_summary.get("episode_id") == 20,
            "Population impact summary does not match the GDACS event.")
    require("per pixel" in population_metadata.get("units", "").lower(),
            "WorldPop unit semantics must identify values per pixel.")
    require(impact_summary.get("no_synthetic_data") is True
            and impact_summary.get("no_poly_global_used") is True,
            "Combined impact summary must exclude synthetic data and Poly_Global.")
    require(grid.get("type") == "FeatureCollection"
            and grid.get("coordinate_reference_system") == "EPSG:4326",
            "Grid must be an EPSG:4326 FeatureCollection.")


def upsert_hazard(
    connection,
    *,
    disaster_id: int,
    source_id_value: int,
    flood: dict,
    flood_metadata: dict,
) -> int:
    feature = flood["features"][0]
    geometry = shapely.geometry.shape(feature["geometry"])
    require(geometry.is_valid and not geometry.is_empty,
            "Selected GDACS Episode 20 Poly_Affected geometry is invalid or empty.")
    require(geometry.geom_type in ("Polygon", "MultiPolygon"),
            "GDACS reported affected-area geometry must be polygonal.")
    external_id = "FL-1104121-EP20-Poly_Affected"
    metadata = {
        "event_id": 1104121,
        "episode_id": 20,
        "polygon_type": "Poly_Affected",
        "semantics": "GDACS analyst-reported affected area",
        "not_observed_inundation": True,
        "source_file": str(FLOOD_PATH.relative_to(ROOT)),
        "source_geometry_url": feature.get("properties", {}).get("source_url"),
        "geometry_investigation": str(
            (FLOOD_PATH.parent / "geometry_investigation.json").relative_to(ROOT)
        ),
        "selected_geometry_metadata": flood_metadata,
    }
    result = connection.execute(
        text(
            """
            INSERT INTO hazard_observations
                (disaster_id, source_id, hazard_type, external_id, geometry,
                 raster_path, metadata_json)
            VALUES
                (:disaster_id, :source_id, 'flood', :external_id,
                 ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326),
                 NULL, :metadata_json)
            ON CONFLICT (source_id, external_id)
                WHERE external_id IS NOT NULL
            DO UPDATE SET
                disaster_id = EXCLUDED.disaster_id,
                hazard_type = EXCLUDED.hazard_type,
                geometry = EXCLUDED.geometry,
                metadata_json = EXCLUDED.metadata_json
            RETURNING id
            """
        ),
        {
            "disaster_id": disaster_id,
            "source_id": source_id_value,
            "external_id": external_id,
            "geometry": json.dumps(feature["geometry"]),
            "metadata_json": json.dumps(metadata, ensure_ascii=False),
        },
    )
    return int(result.scalar_one())


def upsert_population_dataset(
    connection,
    *,
    source_id_value: int,
    metadata: dict,
) -> int:
    with rasterio.open(POPULATION_PATH) as raster:
        require(
            raster.crs is not None
            and CRS.from_user_input(raster.crs).equals(
                CRS.from_epsg(4326),
                ignore_axis_order=True,
            )
            and metadata.get("crs") == "EPSG:4326",
                "Processed WorldPop raster must use EPSG:4326.")
        require(raster.count == 1, "Expected a single-band WorldPop raster.")
        require(raster.width == metadata.get("width")
                and raster.height == metadata.get("height"),
                "Raster dimensions do not match recorded WorldPop metadata.")
        require(raster.nodata == metadata.get("nodata"),
                "Raster NoData value does not match recorded WorldPop metadata.")
        values = raster.read(1, masked=True)
        valid = np.asarray(values.compressed(), dtype=np.float64)
        require(valid.size > 0, "Processed WorldPop raster has no valid pixels.")
        require(bool(np.isfinite(valid).all()) and bool((valid >= 0).all()),
                "Population raster contains invalid or negative population values.")
        require(valid.size == metadata.get("valid_pixel_count"),
                "Valid pixel count does not match recorded WorldPop metadata.")
        require(np.isclose(float(valid.min()), metadata.get("population_min"), rtol=1e-6)
                and np.isclose(float(valid.max()), metadata.get("population_max"), rtol=1e-6),
                "Raster min/max values do not match recorded WorldPop metadata.")
        left, bottom, right, top = raster.bounds
        resolution_x, resolution_y = raster.res

    bounds = [float(left), float(bottom), float(right), float(top)]
    coverage = {
        "type": "Polygon",
        "coordinates": [[
            [bounds[0], bounds[1]],
            [bounds[2], bounds[1]],
            [bounds[2], bounds[3]],
            [bounds[0], bounds[3]],
            [bounds[0], bounds[1]],
        ]],
    }
    result = connection.execute(
        text(
            """
            INSERT INTO population_datasets
                (source_id, source_name, dataset_name, year, source_url, license,
                 resolution_x, resolution_y, crs, units, value_semantics, nodata_value,
                 pixel_dtype, width, height, valid_pixel_count, minimum_value,
                 maximum_value, raster_path, bounds, coverage_geometry, metadata_json)
            VALUES
                (:source_id, :source_name, :dataset_name, :year, :source_url, :license,
                 :resolution_x, :resolution_y, :crs, :units, :value_semantics, :nodata,
                 :pixel_dtype, :width, :height, :valid_pixel_count, :minimum_value,
                 :maximum_value, :raster_path, CAST(:bounds AS JSONB),
                 ST_SetSRID(ST_GeomFromGeoJSON(:coverage), 4326), CAST(:metadata AS JSONB))
            ON CONFLICT (source_name, dataset_name, year) DO UPDATE SET
                source_id = EXCLUDED.source_id,
                source_url = EXCLUDED.source_url,
                license = EXCLUDED.license,
                resolution_x = EXCLUDED.resolution_x,
                resolution_y = EXCLUDED.resolution_y,
                crs = EXCLUDED.crs,
                units = EXCLUDED.units,
                value_semantics = EXCLUDED.value_semantics,
                nodata_value = EXCLUDED.nodata_value,
                pixel_dtype = EXCLUDED.pixel_dtype,
                width = EXCLUDED.width,
                height = EXCLUDED.height,
                valid_pixel_count = EXCLUDED.valid_pixel_count,
                minimum_value = EXCLUDED.minimum_value,
                maximum_value = EXCLUDED.maximum_value,
                raster_path = EXCLUDED.raster_path,
                bounds = EXCLUDED.bounds,
                coverage_geometry = EXCLUDED.coverage_geometry,
                metadata_json = EXCLUDED.metadata_json,
                updated_at = now()
            RETURNING id
            """
        ),
        {
            "source_id": source_id_value,
            "source_name": metadata["source"],
            "dataset_name": metadata["dataset_name"],
            "year": metadata["year"],
            "source_url": metadata["source_url"],
            "license": metadata["license"],
            "resolution_x": abs(resolution_x),
            "resolution_y": abs(resolution_y),
            "crs": metadata["crs"],
            "units": metadata["units"],
            "value_semantics": metadata["raster_value_meaning"],
            "nodata": metadata["nodata"],
            "pixel_dtype": metadata["dtype"],
            "width": metadata["width"],
            "height": metadata["height"],
            "valid_pixel_count": int(valid.size),
            "minimum_value": float(valid.min()),
            "maximum_value": float(valid.max()),
            "raster_path": str(POPULATION_PATH.relative_to(ROOT)),
            "bounds": json.dumps(bounds),
            "coverage": json.dumps(coverage),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        },
    )
    return int(result.scalar_one())


def load_population_exposure(
    connection,
    *,
    population_dataset_id: int,
    disaster_id: int,
    hazard_observation_id: int,
    summary: dict,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO population_exposures
                (population_dataset_id, disaster_id, hazard_observation_id,
                 total_population, affected_population, total_valid_pixels,
                 affected_pixels, analysis_method)
            VALUES
                (:dataset_id, :disaster_id, :hazard_id, :total_population,
                 :affected_population, :total_pixels, :affected_pixels, :method)
            ON CONFLICT (population_dataset_id, disaster_id, hazard_observation_id)
            DO UPDATE SET
                total_population = EXCLUDED.total_population,
                affected_population = EXCLUDED.affected_population,
                total_valid_pixels = EXCLUDED.total_valid_pixels,
                affected_pixels = EXCLUDED.affected_pixels,
                analysis_method = EXCLUDED.analysis_method
            """
        ),
        {
            "dataset_id": population_dataset_id,
            "disaster_id": disaster_id,
            "hazard_id": hazard_observation_id,
            "total_population": summary["total_population"],
            "affected_population": summary["affected_population"],
            "total_pixels": summary["total_valid_pixels"],
            "affected_pixels": summary["affected_pixels"],
            "method": summary["analysis_method"],
        },
    )


def load_building_exposure(
    connection,
    *,
    disaster_id: int,
    source_id_value: int,
    expected_count: int,
) -> int:
    inserted = 0
    insert_statement = text(
        """
        INSERT INTO building_exposure_footprints
            (disaster_id, source_id, source_feature_id, geometry,
             footprint_area_km2, source_properties)
        VALUES
            (:disaster_id, :source_id, :source_feature_id,
             ST_SetSRID(ST_GeomFromWKB(:geometry), 4326),
             ST_Area(ST_Transform(
                 ST_SetSRID(ST_GeomFromWKB(:geometry), 4326), 32645
             )) / 1000000.0,
             CAST(:source_properties AS JSONB))
        ON CONFLICT (disaster_id, source_id, source_feature_id) DO UPDATE SET
            geometry = EXCLUDED.geometry,
            footprint_area_km2 = EXCLUDED.footprint_area_km2,
            source_properties = EXCLUDED.source_properties
        """
    )
    with open_arrow(BUILDINGS_PATH, batch_size=2048) as (arrow_metadata, arrow_stream):
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        geometry_name = arrow_metadata.get("geometry_name")
        if geometry_name not in reader.schema.names:
            geometry_name = next(
                (
                    field.name
                    for field in reader.schema
                    if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)
                ),
                None,
            )
        require(geometry_name is not None, "Cannot identify OSM footprint geometry column.")
        require("osm_id" in reader.schema.names, "OSM feature IDs are missing from impact data.")
        property_names = [
            name for name in ("code", "fclass", "name", "type", "source") if name in reader.schema.names
        ]
        for batch in reader:
            geometries = batch.column(geometry_name).to_pylist()
            osm_ids = batch.column("osm_id").to_pylist()
            require(all(geometry is not None for geometry in geometries),
                    "OSM impact data contains missing geometry.")
            decoded = shapely.from_wkb(geometries)
            valid_mask = shapely.is_valid(decoded) & ~shapely.is_empty(decoded)
            polygon_mask = np.isin(shapely.get_type_id(decoded), [3, 6])
            area_mask = shapely.area(decoded) > 0
            require(bool(np.all(valid_mask & polygon_mask & area_mask)),
                    "OSM impact data contains invalid, empty, non-polygonal or zero-area geometry.")
            rows = []
            for index, (osm_id, geometry) in enumerate(zip(osm_ids, geometries)):
                require(osm_id is not None, "OSM impact feature is missing its OSM ID.")
                props = {"osm_id": str(osm_id)}
                for name in property_names:
                    value = batch.column(name)[index].as_py()
                    if value is not None:
                        props[name] = value
                rows.append(
                    {
                        "disaster_id": disaster_id,
                        "source_id": source_id_value,
                        "source_feature_id": str(osm_id),
                        "geometry": bytes(geometry),
                        "source_properties": json.dumps(props, ensure_ascii=False),
                    }
                )
            connection.execute(insert_statement, rows)
            inserted += len(rows)
    require(inserted == expected_count,
            f"Loaded {inserted} OSM exposure footprints; expected {expected_count}.")
    return inserted


def load_grid_zones(connection, *, disaster_id: int, grid: dict) -> int:
    features = grid.get("features")
    require(isinstance(features, list) and features, "Generated grid has no features.")
    statement = text(
        """
        INSERT INTO impact_zones
            (disaster_id, zone_code, geometry, population_count, building_count,
             road_length_km, hazard_coverage, population_exposure, building_exposure,
             infrastructure_exposure, building_footprint_area_km2, vulnerability_score,
             impact_score, severity_class)
        VALUES
            (:disaster_id, :zone_code,
             ST_SetSRID(ST_GeomFromGeoJSON(:geometry), 4326),
             :population, :building_count, NULL, :disaster_exposure, :population_exposure,
             :building_exposure, :infrastructure_exposure, :building_footprint_area_km2,
             :vulnerability_score, :impact_score, :severity_class)
        ON CONFLICT (disaster_id, zone_code) DO UPDATE SET
            geometry = EXCLUDED.geometry,
            population_count = EXCLUDED.population_count,
            building_count = EXCLUDED.building_count,
            hazard_coverage = EXCLUDED.hazard_coverage,
            population_exposure = EXCLUDED.population_exposure,
            building_exposure = EXCLUDED.building_exposure,
            infrastructure_exposure = EXCLUDED.infrastructure_exposure,
            building_footprint_area_km2 = EXCLUDED.building_footprint_area_km2,
            vulnerability_score = EXCLUDED.vulnerability_score,
            impact_score = EXCLUDED.impact_score,
            severity_class = EXCLUDED.severity_class,
            updated_at = now()
        """
    )
    rows = []
    for feature in features:
        properties = feature.get("properties", {})
        require(properties.get("disaster_external_id") == "GDACS:1104121",
                "Grid cell is not associated with GDACS event 1104121.")
        require(properties.get("hazard_semantics") == "GDACS reported affected area"
                and properties.get("not_observed_inundation") is True,
                "Grid cell hazard semantics are missing or invalid.")
        rows.append(
            {
                "disaster_id": disaster_id,
                "zone_code": properties["zone_id"],
                "geometry": json.dumps(feature["geometry"]),
                "population": properties["population"],
                "building_count": properties["building_count"],
                "disaster_exposure": properties["disaster_exposure"],
                "population_exposure": properties["population_exposure"],
                "building_exposure": properties["building_exposure"],
                "infrastructure_exposure": properties["infrastructure_exposure"],
                "building_footprint_area_km2": properties["building_footprint_area_km2"],
                "vulnerability_score": properties["vulnerability_score"],
                "impact_score": properties["impact_score"],
                "severity_class": properties["severity_class"],
            }
        )
    connection.execute(statement, rows)
    return len(rows)


def main() -> int:
    flood = read_json(FLOOD_PATH)
    flood_metadata = read_json(FLOOD_METADATA_PATH)
    building_summary = read_json(BUILDING_SUMMARY_PATH)
    osm_metadata = read_json(BUILDING_METADATA_PATH)
    population_summary = read_json(POPULATION_SUMMARY_PATH)
    population_metadata = read_json(POPULATION_METADATA_PATH)
    impact_summary = read_json(IMPACT_SUMMARY_PATH)
    grid = read_json(GRID_PATH)
    validate_sources(
        flood,
        flood_metadata,
        building_summary,
        osm_metadata,
        population_summary,
        population_metadata,
        impact_summary,
        grid,
    )

    event_source = flood["features"][0]["properties"].get("source_url")
    osm_hash = file_sha256(BUILDINGS_PATH)
    population_hash = file_sha256(POPULATION_PATH)
    gdacs_hash = file_sha256(FLOOD_PATH)

    with engine.begin() as connection:
        require(
            connection.execute(text("SELECT 1 FROM pg_extension WHERE extname='postgis'")).scalar_one_or_none()
            == 1,
            "PostGIS extension is not installed in the configured database.",
        )
        disaster = connection.execute(
            text(
                "SELECT id FROM disasters "
                "WHERE source = 'GDACS' AND external_id = '1104121'"
            )
        ).scalar_one_or_none()
        require(disaster is not None,
                "GDACS disaster 1104121 is not already present; refusing to invent a disaster record.")
        disaster_id = int(disaster)
        count_zones = int(
            connection.execute(
                text("SELECT count(*) FROM impact_zones WHERE disaster_id = :id"),
                {"id": disaster_id},
            ).scalar_one()
        )
        require(count_zones == len(grid["features"]),
                "Database grid count differs from the generated Step 3 dataset.")

        gdacs_source_id = source_id(
            connection,
            disaster_id=disaster_id,
            source_name="GDACS",
            external_id="FL-1104121-EP20-Poly_Affected",
            source_url=event_source,
            raw_data_path=str(FLOOD_PATH.relative_to(ROOT)),
            payload_hash=gdacs_hash,
            metadata={
                "event_id": 1104121,
                "episode_id": 20,
                "polygon_type": "Poly_Affected",
                "semantic_meaning": "GDACS analyst-reported affected area",
                "not_observed_inundation": True,
                "metadata_file": str(FLOOD_METADATA_PATH.relative_to(ROOT)),
            },
        )
        osm_source_id = source_id(
            connection,
            disaster_id=disaster_id,
            source_name="OSM",
            external_id=f"Geofabrik-Bihar-affected-1104121-{osm_hash}",
            source_url=osm_metadata.get("source_url"),
            raw_data_path=str(BUILDINGS_PATH.relative_to(ROOT)),
            payload_hash=osm_hash,
            metadata={
                "dataset": "OSM building footprint portions within GDACS reported affected area",
                "license": osm_metadata.get("license"),
                "license_url": osm_metadata.get("license_url"),
                "source_metadata_file": str(BUILDING_METADATA_PATH.relative_to(ROOT)),
                "synthetic_generation_used": False,
            },
        )
        worldpop_source_id = source_id(
            connection,
            disaster_id=disaster_id,
            source_name="WorldPop",
            external_id=f"{population_metadata['dataset_doi']}:{population_metadata['year']}",
            source_url=population_metadata["source_url"],
            raw_data_path=str(POPULATION_PATH.relative_to(ROOT)),
            payload_hash=population_hash,
            metadata={
                "documentation_url": population_metadata["documentation_url"],
                "license": population_metadata["license"],
                "license_url": population_metadata["license_url"],
                "source_metadata_file": str(POPULATION_METADATA_PATH.relative_to(ROOT)),
                "units": population_metadata["units"],
                "not_density": True,
            },
        )
        hazard_id = upsert_hazard(
            connection,
            disaster_id=disaster_id,
            source_id_value=gdacs_source_id,
            flood=flood,
            flood_metadata=flood_metadata,
        )
        population_dataset_id = upsert_population_dataset(
            connection,
            source_id_value=worldpop_source_id,
            metadata=population_metadata,
        )
        load_population_exposure(
            connection,
            population_dataset_id=population_dataset_id,
            disaster_id=disaster_id,
            hazard_observation_id=hazard_id,
            summary=population_summary,
        )
        building_count = load_building_exposure(
            connection,
            disaster_id=disaster_id,
            source_id_value=osm_source_id,
            expected_count=building_summary["affected_count"],
        )
        zone_count = load_grid_zones(connection, disaster_id=disaster_id, grid=grid)

        database_count = int(
            connection.execute(
                text(
                    """
                    SELECT count(*) FROM building_exposure_footprints
                    WHERE disaster_id = :disaster_id AND source_id = :source_id
                    """
                ),
                {"disaster_id": disaster_id, "source_id": osm_source_id},
            ).scalar_one()
        )
        require(database_count == building_count,
                f"Database has {database_count} current OSM features; expected {building_count}.")

    result = {
        "status": "loaded",
        "database": "emergency_response",
        "disaster_id": disaster_id,
        "event_id": 1104121,
        "episode_id": 20,
        "polygon_type": "Poly_Affected",
        "provenance_sources": ["GDACS", "OSM", "WorldPop"],
        "hazard_observations": 1,
        "population_datasets": 1,
        "population_exposures": 1,
        "building_exposure_footprints": building_count,
        "grid_zones_upserted": zone_count,
        "infrastructure_assets": 0,
        "infrastructure_note": "No approved infrastructure dataset exists in the validated Step 1-3 inputs.",
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"STEP 4 DATA LOAD BLOCKED: {type(error).__name__}: {error}", file=sys.stderr)
        raise
