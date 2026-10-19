"""Independently validate Step 2D building and population impact outputs."""

import json
import math
import os
import sys
from pathlib import Path

import pyproj

os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import numpy as np
import pyarrow as pa
import pyogrio
import rasterio
import shapely
from pyogrio.raw import open_arrow
from pyproj import CRS, Transformer
from rasterio.features import geometry_mask
from rasterio.windows import Window
from shapely.geometry import mapping, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_PATH = ROOT / "data/external/boundaries/bihar_boundary.geojson"
FLOOD_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent.geojson"
FLOOD_METADATA_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent_metadata.json"
BUILDINGS_PATH = ROOT / "data/processed/osm/bihar_buildings.geojson"
BUILDINGS_METADATA_PATH = ROOT / "data/processed/osm/bihar_buildings_metadata.json"
POPULATION_PATH = ROOT / "data/processed/population/bihar_population.tif"
POPULATION_METADATA_PATH = ROOT / "data/processed/population/bihar_population_metadata.json"
OUTPUT_DIR = ROOT / "data/processed/impact"
AFFECTED_BUILDINGS_PATH = OUTPUT_DIR / "affected_buildings.geojson"
BUILDING_SUMMARY_PATH = OUTPUT_DIR / "building_impact_summary.json"
POPULATION_SUMMARY_PATH = OUTPUT_DIR / "population_impact_summary.json"
SUMMARY_PATH = OUTPUT_DIR / "impact_assessment_summary.json"
AREA_CRS = "EPSG:32645"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    require(path.is_file(), f"Required file is missing: {path}")
    try:
        with path.open(encoding="utf-8") as source:
            result = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid JSON at {path}: {error}") from error
    require(isinstance(result, dict), f"Expected a JSON object in {path}.")
    return result


def load_geometry(path: Path, label: str):
    document = read_json(path)
    require(document.get("type") == "FeatureCollection", f"{label} is not a FeatureCollection.")
    features = document.get("features")
    require(isinstance(features, list) and features, f"{label} contains no features.")
    geometry = unary_union([shape(feature["geometry"]) for feature in features])
    require(geometry.is_valid and not geometry.is_empty, f"{label} geometry is invalid or empty.")
    return document, geometry


def geometry_column_name(metadata, schema) -> str:
    candidate = metadata.get("geometry_name")
    if candidate in schema.names:
        return candidate
    for name in ("geom", "wkb_geometry", "geometry"):
        if name in schema.names:
            return name
    binary = [
        field.name for field in schema
        if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)
    ]
    require(len(binary) == 1, f"Cannot identify GeoJSON geometry column in {schema}.")
    return binary[0]


def compare(actual, expected, label: str, rel_tol: float = 1e-8) -> None:
    require(isinstance(expected, (int, float)) and math.isfinite(expected),
            f"{label} is missing or non-numeric in summary.")
    require(math.isclose(actual, expected, rel_tol=rel_tol, abs_tol=1e-8),
            f"{label} differs from independent recalculation: {actual} vs {expected}.")


def excess_area_m2(geometry, container, transformer) -> float:
    outside = geometry.difference(container)
    if outside.is_empty:
        return 0.0
    projected_outside = shapely.transform(outside, transformer.transform, interleaved=False)
    return float(shapely.area(projected_outside))


def validate_buildings(boundary, affected_area, combined) -> dict:
    require(BUILDINGS_PATH.is_file(), "Original OSM building input is missing.")
    require(AFFECTED_BUILDINGS_PATH.is_file(), "Affected building GeoJSON is missing.")
    source_metadata = read_json(BUILDINGS_METADATA_PATH)
    require(source_metadata.get("synthetic_generation_used") is False, "OSM source metadata does not rule out synthetic data.")
    require("OpenStreetMap" in source_metadata.get("source", "") and "Geofabrik" in source_metadata.get("source", ""),
            "OSM source provenance is missing.")
    area_transformer = Transformer.from_crs("EPSG:4326", AREA_CRS, always_xy=True)
    shapely.prepare(affected_area)
    shapely.prepare(boundary)
    total_count = affected_count = 0
    total_area_m2 = affected_area_m2 = 0.0
    with open_arrow(BUILDINGS_PATH, batch_size=8192) as context:
        source_metadata_arrow, arrow_stream = context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        geom_name = geometry_column_name(source_metadata_arrow, reader.schema)
        for batch in reader:
            geometries = shapely.from_wkb(batch.column(geom_name).to_pylist())
            require(bool(np.all(shapely.is_valid(geometries) & ~shapely.is_empty(geometries))),
                    "Original building input has invalid or empty geometries.")
            total_count += len(geometries)
            require(bool(np.all(shapely.covers(boundary, geometries))),
                    "Original building input has geometry outside Bihar.")
            projected = shapely.transform(geometries, area_transformer.transform, interleaved=False)
            footprint_areas = shapely.area(projected)
            total_area_m2 += float(np.sum(footprint_areas, dtype=np.float64))
            covered = shapely.covers(affected_area, geometries)
            covered_areas = footprint_areas[covered]
            affected_area_m2 += float(np.sum(covered_areas, dtype=np.float64))
            affected_count += int(np.count_nonzero(covered_areas > 0))
            partial_indices = np.flatnonzero(~covered)
            intersections = shapely.intersection(geometries[partial_indices], affected_area)
            intersections = shapely.intersection(intersections, affected_area)
            projected_intersections = shapely.transform(intersections, area_transformer.transform, interleaved=False)
            partial_areas = shapely.area(projected_intersections)
            affected_area_m2 += float(np.sum(partial_areas, dtype=np.float64))
            affected_count += int(np.count_nonzero(partial_areas > 0))

    with AFFECTED_BUILDINGS_PATH.open("r", encoding="utf-8") as source:
        header = source.read(1024)
    require('"type":"FeatureCollection"' in header.replace(" ", ""),
            "Affected building output is not a GeoJSON FeatureCollection.")
    require('"coordinate_reference_system":"EPSG:4326"' in header.replace(" ", ""),
            "Affected building output does not record EPSG:4326.")
    output_info = pyogrio.read_info(AFFECTED_BUILDINGS_PATH)
    require(output_info.get("driver") == "GeoJSON", "Affected building output is not valid GeoJSON.")
    require(CRS.from_user_input(output_info.get("crs")).equals(CRS.from_epsg(4326), ignore_axis_order=True),
            "Affected building GeoJSON CRS is not EPSG:4326.")
    output_count = 0
    output_area_m2 = 0.0
    osm_ids = set()
    with open_arrow(AFFECTED_BUILDINGS_PATH, batch_size=8192) as context:
        output_metadata, arrow_stream = context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        geom_name = geometry_column_name(output_metadata, reader.schema)
        require("osm_id" in reader.schema.names, "Affected building output does not preserve OSM IDs.")
        for batch in reader:
            geometries = shapely.from_wkb(batch.column(geom_name).to_pylist())
            require(bool(np.all(shapely.is_valid(geometries) & ~shapely.is_empty(geometries))),
                    "Affected building output contains invalid or empty geometries.")
            require(bool(np.all(np.isin(shapely.get_type_id(geometries), [3, 6]))),
                    "Affected building output contains non-polygonal geometry.")
            outside_bihar_candidates = np.flatnonzero(~shapely.covers(boundary, geometries))
            require(all(excess_area_m2(geometries[index], boundary, area_transformer) <= 1e-4
                        for index in outside_bihar_candidates),
                    "Affected building output extends materially outside Bihar.")
            outside_affected_candidates = np.flatnonzero(~shapely.covers(affected_area, geometries))
            require(all(excess_area_m2(geometries[index], affected_area, area_transformer) <= 1e-4
                        for index in outside_affected_candidates),
                    "Affected building output extends materially outside the GDACS affected area.")
            output_count += len(geometries)
            projected = shapely.transform(geometries, area_transformer.transform, interleaved=False)
            output_area_m2 += float(np.sum(shapely.area(projected), dtype=np.float64))
            osm_ids.update(str(identifier) for identifier in batch.column("osm_id").to_pylist())
    require(output_count == len(osm_ids), "Affected buildings contain duplicate OSM IDs; count would double-count features.")
    require(total_count > 0 and output_count <= total_count, "Affected building count exceeds or has no total buildings.")
    require(affected_count == output_count, "Affected output feature count differs from positive-area source overlaps.")
    require(total_area_m2 > 0 and affected_area_m2 <= total_area_m2 * (1 + 1e-8),
            "Affected footprint area exceeds total footprint area.")
    building_summary = read_json(BUILDING_SUMMARY_PATH)
    for key, value in {
        "total_count": total_count,
        "affected_count": affected_count,
        "total_footprint_area_km2": total_area_m2 / 1_000_000,
        "affected_footprint_area_km2": affected_area_m2 / 1_000_000,
    }.items():
        compare(value, building_summary.get(key), f"Building summary {key}")
        compare(value, combined["buildings"].get(key), f"Combined building {key}")
    affected_percent = affected_count / total_count * 100
    footprint_percent = affected_area_m2 / total_area_m2 * 100
    compare(affected_percent, building_summary.get("affected_percent"), "Building affected percentage")
    compare(footprint_percent, building_summary.get("affected_footprint_percent"), "Footprint affected percentage")
    require(0 <= affected_percent <= 100 and 0 <= footprint_percent <= 100,
            "Building impact percentages are outside 0-100.")
    compare(output_area_m2, affected_area_m2, "Affected building output area")
    return {
        "total_count": total_count,
        "affected_count": affected_count,
        "affected_percent": affected_percent,
        "total_footprint_area_km2": total_area_m2 / 1_000_000,
        "affected_footprint_area_km2": affected_area_m2 / 1_000_000,
        "affected_footprint_percent": footprint_percent,
    }


def validate_population(boundary, affected_area, combined) -> dict:
    require(POPULATION_PATH.is_file(), "Population raster input is missing.")
    population_metadata = read_json(POPULATION_METADATA_PATH)
    require("WorldPop" in population_metadata.get("source", ""), "Population source is not identified.")
    require(population_metadata.get("year") is not None, "Population year is not documented.")
    require("people per pixel" in population_metadata.get("units", "").lower(),
            "Population units do not identify people per pixel.")
    require(population_metadata.get("nodata") is not None, "Population NoData is not documented.")
    total_population = affected_population = 0.0
    total_valid = affected_pixels = 0
    with rasterio.open(POPULATION_PATH) as raster:
        require(raster.crs is not None, "Population raster has no CRS.")
        require(CRS.from_user_input(raster.crs).equals(CRS.from_epsg(4326), ignore_axis_order=True),
                "Population raster CRS is not EPSG:4326.")
        require(raster.nodata is not None and raster.nodata == population_metadata["nodata"],
                "Population NoData is missing or differs from documentation.")
        require(raster.count == 1, "Population raster must contain exactly one band.")
        require("WorldPop" in raster.tags().get("source", ""), "Population raster WorldPop tag is missing.")
        require("people per pixel" in raster.tags().get("units", "").lower(),
                "Population raster unit tag is missing.")
        for row_start in range(0, raster.height, 128):
            rows = min(128, raster.height - row_start)
            window = Window(0, row_start, raster.width, rows)
            values = raster.read(1, window=window)
            transform = raster.window_transform(window)
            in_bihar = geometry_mask([mapping(boundary)], out_shape=values.shape, transform=transform,
                                     all_touched=False, invert=True)
            in_affected = geometry_mask([mapping(affected_area)], out_shape=values.shape, transform=transform,
                                        all_touched=False, invert=True)
            valid = np.isfinite(values) & (values != raster.nodata)
            require(not bool(np.any(valid & ~in_bihar)), "Valid population pixels lie outside Bihar.")
            require(bool(np.all(values[valid] >= 0)), "Valid population raster contains negative values.")
            valid_values = values[valid].astype("float64", copy=False)
            total_valid += int(valid_values.size)
            total_population += float(np.sum(valid_values, dtype=np.float64))
            selected = valid & in_affected
            selected_values = values[selected].astype("float64", copy=False)
            affected_pixels += int(selected_values.size)
            affected_population += float(np.sum(selected_values, dtype=np.float64))
    require(total_valid > 0 and 0 <= affected_pixels <= total_valid, "Population pixel counts are invalid.")
    require(0 <= affected_population <= total_population * (1 + 1e-10),
            "Affected population exceeds the total represented population.")
    affected_percent = affected_population / total_population * 100
    require(0 <= affected_percent <= 100, "Population affected percentage is outside 0-100.")
    population_summary = read_json(POPULATION_SUMMARY_PATH)
    for key, value in {
        "total_population": total_population,
        "affected_population": affected_population,
        "total_valid_pixels": total_valid,
        "affected_pixels": affected_pixels,
    }.items():
        compare(value, population_summary.get(key), f"Population summary {key}")
        compare(value, combined["population"].get(key), f"Combined population {key}")
    compare(affected_percent, population_summary.get("affected_percent"), "Population affected percentage")
    require(population_summary.get("units") == population_metadata.get("units"),
            "Population summary unit semantics differ from source metadata.")
    require(population_summary.get("nodata") == population_metadata.get("nodata"),
            "Population summary NoData differs from source metadata.")
    return {
        "total_population": total_population,
        "affected_population": affected_population,
        "affected_percent": affected_percent,
        "total_valid_pixels": total_valid,
        "affected_pixels": affected_pixels,
    }


def main() -> int:
    try:
        boundary_document, boundary = load_geometry(BOUNDARY_PATH, "Bihar boundary")
        boundary_crs = boundary_document.get("crs", {}).get("properties", {}).get("name")
        require(boundary_crs in (None, "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84"),
                f"Unexpected Bihar boundary CRS {boundary_crs}.")
        flood_document, affected_area = load_geometry(FLOOD_PATH, "GDACS reported affected area")
        flood_metadata = read_json(FLOOD_METADATA_PATH)
        require(flood_document.get("coordinate_reference_system") == "EPSG:4326",
                "GDACS geometry CRS is not documented as EPSG:4326.")
        require(flood_metadata.get("event_id") == 1104121 and flood_metadata.get("selected_episode_id") == 20,
                "GDACS event/episode selection is not event 1104121 episode 20.")
        require(flood_metadata.get("selected_polygon_type") == "Poly_Affected",
                "Selected GDACS geometry is not Poly_Affected.")
        require(flood_metadata.get("not_observed_inundation") is True
                and "analyst-reported affected area" in flood_metadata.get("geometry_semantics", "").lower(),
                "GDACS semantics do not clearly identify an analyst-reported affected area.")
        require(boundary.covers(affected_area), "Selected GDACS area is not contained within Bihar.")
        combined = read_json(SUMMARY_PATH)
        require(combined.get("event_id") == 1104121, "Combined summary has wrong event ID.")
        require(combined.get("episode_id") == 20, "Combined summary has wrong episode.")
        require(combined.get("polygon_type") == "Poly_Affected", "Combined summary uses wrong polygon type.")
        require(combined.get("hazard_semantics") == "GDACS reported affected area",
                "Combined summary does not document GDACS semantics.")
        require(combined.get("not_observed_inundation") is True, "Summary does not rule out observed inundation claims.")
        require(combined.get("no_poly_global_used") is True, "Summary does not explicitly exclude Poly_Global.")
        require(combined.get("no_synthetic_data") is True, "Summary does not explicitly exclude synthetic data.")

        buildings = validate_buildings(boundary, affected_area, combined)
        population = validate_population(boundary, affected_area, combined)
        require("reported affected area" in json.dumps(combined).lower(),
                "Combined summary lacks the required reported-affected-area wording.")
        require(any(
            "does not establish actual flooding or inundation" in item.lower()
            for item in combined.get("limitations", [])
        ), "Combined summary lacks an explicit limitation against interpreting this as actual inundation.")
        print(
            "STEP 2D IMPACT ASSESSMENT\n\n"
            f"Event: {combined['event_id']}\n"
            f"Episode: {combined['episode_id']}\n"
            "Hazard geometry: GDACS Poly_Affected - reported affected area "
            "(not observed inundation)\n\n"
            "Buildings:\n"
            f"Total: {buildings['total_count']:,}\n"
            f"Within reported affected area: {buildings['affected_count']:,}\n"
            f"Percentage: {buildings['affected_percent']:.6f}%\n"
            f"Total footprint area: {buildings['total_footprint_area_km2']:.9f} km2\n"
            "Affected footprint area: "
            f"{buildings['affected_footprint_area_km2']:.9f} km2\n\n"
            "Population:\n"
            f"Total: {population['total_population']:,.2f} estimated people\n"
            "Within reported affected area: "
            f"{population['affected_population']:,.2f} estimated people\n"
            f"Percentage: {population['affected_percent']:.6f}%\n"
            f"Valid pixels: {population['total_valid_pixels']:,}\n"
            f"Affected pixels: {population['affected_pixels']:,}\n\n"
            "Validation: PASSED\n"
            "STEP 2D COMPLETE"
        )
        return 0
    except Exception as error:
        print(f"STEP 2D VALIDATION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
