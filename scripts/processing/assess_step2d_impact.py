"""Calculate building and population exposure within the GDACS reported affected area."""

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
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {error}") from error
    require(isinstance(value, dict), f"Expected a JSON object in {path}.")
    return value


def load_polygon(path: Path, label: str):
    document = read_json(path)
    require(document.get("type") == "FeatureCollection", f"{label} is not a FeatureCollection.")
    features = document.get("features")
    require(isinstance(features, list) and features, f"{label} has no features.")
    geometries = [shape(feature["geometry"]) for feature in features]
    geometry = unary_union(geometries)
    require(not geometry.is_empty and geometry.is_valid, f"{label} geometry is empty or invalid.")
    return document, geometry


def geometry_column_name(metadata, schema) -> str:
    candidate = metadata.get("geometry_name")
    if candidate in schema.names:
        return candidate
    for name in ("geom", "wkb_geometry", "geometry"):
        if name in schema.names:
            return name
    binary_columns = [
        field.name
        for field in schema
        if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)
    ]
    require(len(binary_columns) == 1, f"Cannot identify geometry column in Arrow schema {schema}.")
    return binary_columns[0]


def json_value(value):
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "as_py"):
        return json_value(value.as_py())
    if hasattr(value, "item"):
        return json_value(value.item())
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    raise TypeError(f"Unsupported OSM property value: {type(value).__name__}")


def excess_area_m2(geometry, container, transformer) -> float:
    outside = geometry.difference(container)
    if outside.is_empty:
        return 0.0
    projected_outside = shapely.transform(outside, transformer.transform, interleaved=False)
    return float(shapely.area(projected_outside))


def calculate_building_impact(boundary, affected_area, flood_metadata) -> dict:
    require(BUILDINGS_PATH.is_file(), f"Building dataset is missing: {BUILDINGS_PATH}")
    building_metadata = read_json(BUILDINGS_METADATA_PATH)
    require(building_metadata.get("synthetic_generation_used") is False, "OSM metadata does not rule out synthetic buildings.")
    require("OpenStreetMap" in building_metadata.get("source", "") and "Geofabrik" in building_metadata.get("source", ""),
            "Building source is not documented as OSM-derived Geofabrik data.")
    info = pyogrio.read_info(BUILDINGS_PATH)
    require(info.get("driver") == "GeoJSON", "Building input is not valid GeoJSON.")
    require(CRS.from_user_input(info.get("crs")).to_epsg() == 4326, "Building input must be EPSG:4326.")

    transformer = Transformer.from_crs("EPSG:4326", AREA_CRS, always_xy=True)
    shapely.prepare(affected_area)
    total_count = affected_count = 0
    total_area_m2 = affected_area_m2 = 0.0
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    temporary = AFFECTED_BUILDINGS_PATH.with_name(AFFECTED_BUILDINGS_PATH.name + ".part")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        target.write('{"type":"FeatureCollection","name":"OSM building portions within GDACS reported affected area",')
        target.write('"coordinate_reference_system":"EPSG:4326","features":[')
        first_feature = True
        with open_arrow(BUILDINGS_PATH, batch_size=8192) as arrow_context:
            source_metadata, arrow_stream = arrow_context
            reader = pa.RecordBatchReader.from_stream(arrow_stream)
            geom_name = geometry_column_name(source_metadata, reader.schema)
            property_names = [name for name in reader.schema.names if name != geom_name]
            require("osm_id" in property_names, "Building data does not preserve OSM IDs.")
            for batch in reader:
                source_wkb = batch.column(geom_name).to_pylist()
                require(all(item is not None for item in source_wkb), "Building input contains missing geometries.")
                geometries = shapely.from_wkb(source_wkb)
                require(bool(np.all(shapely.is_valid(geometries) & ~shapely.is_empty(geometries))),
                        "Building input contains invalid or empty geometries.")
                require(bool(np.all(shapely.covers(boundary, geometries))),
                        "Building input includes a footprint outside Bihar.")
                total_count += len(geometries)
                projected = shapely.transform(geometries, transformer.transform, interleaved=False)
                footprint_areas = shapely.area(projected)
                total_area_m2 += float(np.sum(footprint_areas, dtype=np.float64))
                covered = shapely.covers(affected_area, geometries)
                covered_indices = np.flatnonzero(covered & (footprint_areas > 0))
                partial_indices = np.flatnonzero(~covered)
                partial_intersections = shapely.intersection(
                    geometries[partial_indices], affected_area
                )
                partial_intersections = shapely.intersection(
                    partial_intersections, affected_area
                )
                projected_partial = shapely.transform(
                    partial_intersections, transformer.transform, interleaved=False
                )
                partial_areas = shapely.area(projected_partial)
                positive_partial = np.flatnonzero(partial_areas > 0)
                identifiers = batch.column("osm_id").to_pylist()
                property_values = {
                    name: batch.column(name).to_pylist()
                    for name in property_names
                }
                for index in covered_indices:
                    geometry = geometries[index]
                    require(geometry.geom_type in ("Polygon", "MultiPolygon"),
                            f"Contained OSM feature {identifiers[index]} is not polygonal.")
                    feature_properties = {
                        name: json_value(values[index])
                        for name, values in property_values.items()
                    }
                    feature_json = {
                        "type": "Feature",
                        "properties": feature_properties,
                        "geometry": json.loads(shapely.to_geojson(geometry)),
                    }
                    if not first_feature:
                        target.write(",")
                    target.write(json.dumps(feature_json, ensure_ascii=False, separators=(",", ":")))
                    first_feature = False
                    affected_count += 1
                    affected_area_m2 += float(footprint_areas[index])
                for partial_index in positive_partial:
                    source_index = int(partial_indices[partial_index])
                    geometry = partial_intersections[partial_index]
                    require(geometry.is_valid and not geometry.is_empty,
                            f"Intersection for OSM feature {identifiers[source_index]} is invalid or empty.")
                    require(geometry.geom_type in ("Polygon", "MultiPolygon"),
                            f"Intersection for OSM feature {identifiers[source_index]} is not polygonal.")
                    require(excess_area_m2(geometry, boundary, transformer) <= 1e-4,
                            f"Affected footprint {identifiers[source_index]} extends materially outside Bihar.")
                    require(excess_area_m2(geometry, affected_area, transformer) <= 1e-4,
                            f"Affected footprint {identifiers[source_index]} extends materially outside the selected GDACS area.")
                    feature_properties = {
                        name: json_value(values[source_index])
                        for name, values in property_values.items()
                    }
                    feature_json = {
                        "type": "Feature",
                        "properties": feature_properties,
                        "geometry": json.loads(shapely.to_geojson(geometry)),
                    }
                    if not first_feature:
                        target.write(",")
                    target.write(json.dumps(feature_json, ensure_ascii=False, separators=(",", ":")))
                    first_feature = False
                    affected_count += 1
                    affected_area_m2 += float(partial_areas[partial_index])
        target.write("]}")
    require(total_count == building_metadata.get("feature_count"),
            "Read building count differs from validated OSM metadata.")
    require(total_count > 0 and total_area_m2 > 0, "Building dataset has no positive footprint area.")
    require(affected_count > 0 and affected_area_m2 > 0, "No buildings have positive-area overlap with the reported affected area.")
    temporary.replace(AFFECTED_BUILDINGS_PATH)
    affected_percent = affected_count / total_count * 100.0
    footprint_percent = affected_area_m2 / total_area_m2 * 100.0
    summary = {
        "event_id": 1104121,
        "episode_id": 20,
        "polygon_type": "Poly_Affected",
        "hazard_semantics": "GDACS reported affected area",
        "not_observed_inundation": True,
        "source_buildings": building_metadata["source"],
        "source_flood": flood_metadata.get("source", "GDACS"),
        "analysis_crs": AREA_CRS,
        "area_method": "Each original OSM footprint is intersected with the selected affected-area geometry; areas are measured in EPSG:32645. Affected building count includes unique OSM features with positive-area overlap. Containment checks allow at most 0.0001 m² of numerical overlay residue.",
        "output_geometry_semantics": "Polygon portions of unique OSM building footprints within the GDACS reported affected area.",
        "total_count": total_count,
        "affected_count": affected_count,
        "affected_percent": affected_percent,
        "total_footprint_area_km2": total_area_m2 / 1_000_000,
        "affected_footprint_area_km2": affected_area_m2 / 1_000_000,
        "affected_footprint_percent": footprint_percent,
        "output_file": AFFECTED_BUILDINGS_PATH.name,
        "limitations": [
            "GDACS geometry denotes an analyst-reported affected area, not observed inundation.",
            "OSM building footprint completeness is variable; unrecorded buildings are not included.",
            "Footprint area sums features individually and may double-count overlapping OSM geometries.",
        ],
    }
    BUILDING_SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def calculate_population_impact(boundary, affected_area, flood_metadata) -> dict:
    require(POPULATION_PATH.is_file(), f"Population raster is missing: {POPULATION_PATH}")
    population_metadata = read_json(POPULATION_METADATA_PATH)
    require(population_metadata.get("source") == "WorldPop, University of Southampton",
            "Population raster source is not identified as WorldPop.")
    require(population_metadata.get("year") == 2020, "Population source year is missing or unexpected.")
    units = population_metadata.get("units", "")
    require("people per pixel" in units.lower(), "Population units do not document people per pixel.")
    nodata_documented = population_metadata.get("nodata")
    valid_pixels = affected_pixels = 0
    total_population = affected_population = 0.0
    with rasterio.open(POPULATION_PATH) as raster:
        require(raster.crs is not None and CRS.from_user_input(raster.crs).equals(
            CRS.from_epsg(4326), ignore_axis_order=True), "Population raster CRS is not EPSG:4326.")
        require(raster.nodata is not None and raster.nodata == nodata_documented,
                "Population raster NoData is missing or inconsistent with metadata.")
        require(raster.count == 1, "Population raster must contain exactly one band.")
        raster_tags = raster.tags()
        require("WorldPop" in raster_tags.get("source", ""), "WorldPop provenance tag is missing from raster.")
        require("people per pixel" in raster_tags.get("units", "").lower(),
                "People-per-pixel raster unit tag is missing.")
        boundary_mask_geometry = [mapping(boundary)]
        affected_mask_geometry = [mapping(affected_area)]
        for row_start in range(0, raster.height, 128):
            rows = min(128, raster.height - row_start)
            window = Window(0, row_start, raster.width, rows)
            values = raster.read(1, window=window)
            transform = raster.window_transform(window)
            inside_bihar = geometry_mask(
                boundary_mask_geometry,
                out_shape=values.shape,
                transform=transform,
                all_touched=False,
                invert=True,
            )
            inside_affected_area = geometry_mask(
                affected_mask_geometry,
                out_shape=values.shape,
                transform=transform,
                all_touched=False,
                invert=True,
            )
            valid = np.isfinite(values) & (values != raster.nodata)
            require(not bool(np.any(valid & ~inside_bihar)),
                    "Population input contains valid cells outside the Bihar boundary mask.")
            require(bool(np.all(values[valid] >= 0)), "Population raster contains negative valid values.")
            selected = values[valid].astype("float64", copy=False)
            valid_pixels += int(selected.size)
            total_population += float(np.sum(selected, dtype=np.float64))
            affected = valid & inside_affected_area
            affected_values = values[affected].astype("float64", copy=False)
            affected_pixels += int(affected_values.size)
            affected_population += float(np.sum(affected_values, dtype=np.float64))
    require(valid_pixels > 0 and total_population > 0, "Population raster has no positive valid Bihar population.")
    require(affected_pixels > 0 and affected_population >= 0,
            "No valid population pixels fall within the GDACS reported affected area.")
    summary = {
        "event_id": 1104121,
        "episode_id": 20,
        "polygon_type": "Poly_Affected",
        "hazard_semantics": "GDACS reported affected area",
        "not_observed_inundation": True,
        "source_population": population_metadata["source"],
        "population_dataset": population_metadata["dataset_name"],
        "year": population_metadata["year"],
        "units": units,
        "nodata": raster.nodata,
        "analysis_method": "Each raster pixel is counted at most once using a center-in-polygon mask (all_touched=False); native grid values are summed as estimated people per pixel without density conversion or resampling.",
        "total_population": total_population,
        "affected_population": affected_population,
        "affected_percent": affected_population / total_population * 100.0,
        "total_valid_pixels": valid_pixels,
        "affected_pixels": affected_pixels,
        "pixel_selection_rule": "A valid Bihar raster pixel is affected when its center lies within the selected GDACS reported affected-area polygon.",
        "limitations": [
            "GDACS geometry denotes an analyst-reported affected area, not observed inundation.",
            "WorldPop values are modelled estimated people per pixel, not people per km².",
            "NoData cells represent areas modelled as unsettled and are excluded, not treated as zero.",
            "Population estimates are modelled data, not individual counts.",
        ],
    }
    POPULATION_SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    try:
        boundary_document, boundary = load_polygon(BOUNDARY_PATH, "Bihar boundary")
        boundary_crs = boundary_document.get("crs", {}).get("properties", {}).get("name")
        require(boundary_crs in (None, "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84"),
                f"Unsupported boundary CRS: {boundary_crs}")
        flood_document, affected_area = load_polygon(FLOOD_PATH, "GDACS affected area")
        require(flood_document.get("coordinate_reference_system") == "EPSG:4326",
                "GDACS affected-area file does not identify EPSG:4326.")
        flood_metadata = read_json(FLOOD_METADATA_PATH)
        require(flood_metadata.get("event_id") == 1104121 and flood_metadata.get("selected_episode_id") == 20,
                "Selected GDACS event or episode does not match 1104121 / 20.")
        require(flood_metadata.get("geometry_semantics") == "GDACS analyst-reported affected area",
                "GDACS geometry semantics are not the expected analyst-reported affected area.")
        require(flood_metadata.get("not_observed_inundation") is True,
                "GDACS metadata does not explicitly rule out observed inundation interpretation.")
        require(boundary.covers(affected_area), "GDACS reported affected area extends outside Bihar.")

        buildings = calculate_building_impact(boundary, affected_area, flood_metadata)
        population = calculate_population_impact(boundary, affected_area, flood_metadata)
        combined = {
            "event_id": 1104121,
            "episode_id": 20,
            "polygon_type": "Poly_Affected",
            "hazard_semantics": "GDACS reported affected area",
            "not_observed_inundation": True,
            "gdacs_source": flood_metadata.get("gdacs_geometry_url", flood_metadata.get("source")),
            "analysis_crs": AREA_CRS,
            "no_poly_global_used": True,
            "no_synthetic_data": True,
            "buildings": {
                key: buildings[key]
                for key in (
                    "total_count",
                    "affected_count",
                    "affected_percent",
                    "total_footprint_area_km2",
                    "affected_footprint_area_km2",
                    "affected_footprint_percent",
                )
            },
            "population": {
                key: population[key]
                for key in (
                    "total_population",
                    "affected_population",
                    "affected_percent",
                    "total_valid_pixels",
                    "affected_pixels",
                )
            },
            "outputs": {
                "affected_buildings": AFFECTED_BUILDINGS_PATH.name,
                "building_summary": BUILDING_SUMMARY_PATH.name,
                "population_summary": POPULATION_SUMMARY_PATH.name,
            },
            "limitations": [
                "This assessment measures buildings and modelled population within the GDACS reported affected area; it does not establish actual flooding or inundation.",
                "Building counts and footprint areas represent mapped OSM features only.",
                "Population values are modelled WorldPop estimates in people per pixel; NoData cells are excluded.",
            ],
        }
        SUMMARY_PATH.write_text(json.dumps(combined, indent=2), encoding="utf-8")
        print(json.dumps({
            "status": "calculated",
            "event": combined["event_id"],
            "episode": combined["episode_id"],
            "hazard_geometry": combined["hazard_semantics"],
            "buildings": combined["buildings"],
            "population": combined["population"],
            "output": str(SUMMARY_PATH.relative_to(ROOT)),
        }, indent=2))
        return 0
    except Exception as error:
        print(f"STEP 2D PROCESSING FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
