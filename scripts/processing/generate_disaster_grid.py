"""Generate a projected regular grid with Step 2D impacts and baseline severity."""

import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import sys

import pyproj

os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import numpy as np
import pyarrow as pa
import rasterio
import shapely
from pyogrio.raw import open_arrow
from pyproj import CRS, Transformer
from rasterio.features import geometry_mask
from rasterio.windows import Window
from shapely.geometry import mapping, shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from geoalchemy2.shape import from_shape
from sqlalchemy import delete, select

from app.db.session import SessionLocal
from app.models.disaster import Disaster
from app.models.impact_zone import ImpactZone
from app.services.zone_severity import (
    calculate_baseline_score,
    classify_severity,
    normalize_by_maximum,
)


FLOOD_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent.geojson"
FLOOD_METADATA_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent_metadata.json"
BUILDINGS_PATH = ROOT / "data/processed/impact/affected_buildings.geojson"
BUILDING_SUMMARY_PATH = ROOT / "data/processed/impact/building_impact_summary.json"
POPULATION_PATH = ROOT / "data/processed/population/bihar_population.tif"
POPULATION_SUMMARY_PATH = ROOT / "data/processed/impact/population_impact_summary.json"
COMBINED_IMPACT_PATH = ROOT / "data/processed/impact/impact_assessment_summary.json"
OUTPUT_DIR = ROOT / "data/processed/zones"
GRID_PATH = OUTPUT_DIR / "disaster_grid.geojson"
SUMMARY_PATH = OUTPUT_DIR / "zone_severity_summary.json"
METADATA_PATH = OUTPUT_DIR / "zone_generation_metadata.json"
ANALYSIS_CRS = "EPSG:32645"
STORAGE_CRS = "EPSG:4326"
ZONE_CODE_PREFIX = "GDACS-1104121-GRID-"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    require(path.is_file(), f"Required input is missing: {path}")
    try:
        with path.open(encoding="utf-8") as source:
            result = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {error}") from error
    require(isinstance(result, dict), f"Expected a JSON object in {path}.")
    return result


def geometry_column_name(metadata, schema) -> str:
    candidate = metadata.get("geometry_name")
    if candidate in schema.names:
        return candidate
    for name in ("geom", "wkb_geometry", "geometry"):
        if name in schema.names:
            return name
    binary = [
        field.name
        for field in schema
        if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)
    ]
    require(len(binary) == 1, f"Cannot identify geometry field in Arrow schema {schema}.")
    return binary[0]


def read_feature_collection(path: Path, label: str):
    document = read_json(path)
    require(document.get("type") == "FeatureCollection", f"{label} is not a FeatureCollection.")
    features = document.get("features")
    require(isinstance(features, list) and features, f"{label} has no features.")
    geometries = [shape(feature["geometry"]) for feature in features]
    geometry = shapely.union_all(geometries)
    require(geometry.is_valid and not geometry.is_empty, f"{label} geometry is invalid or empty.")
    return document, geometry, features


def validate_inputs() -> tuple:
    flood_document, affected_area, flood_features = read_feature_collection(
        FLOOD_PATH, "GDACS affected area"
    )
    require(flood_document.get("coordinate_reference_system") == STORAGE_CRS,
            "GDACS affected-area CRS must be EPSG:4326.")
    feature_properties = flood_features[0].get("properties", {})
    require(feature_properties.get("event_id") == 1104121, "GDACS event ID is not 1104121.")
    require(feature_properties.get("episode_id") == 20, "GDACS episode is not 20.")
    require(feature_properties.get("polygon_type") == "Poly_Affected",
            "Grid source must be GDACS Poly_Affected.")
    flood_metadata = read_json(FLOOD_METADATA_PATH)
    require(flood_metadata.get("event_id") == 1104121
            and flood_metadata.get("selected_episode_id") == 20
            and flood_metadata.get("selected_polygon_type") == "Poly_Affected",
            "Step 1 metadata does not confirm the selected event, episode, and polygon type.")
    require(flood_metadata.get("not_observed_inundation") is True,
            "GDACS metadata must document that this is not observed inundation.")

    building_summary = read_json(BUILDING_SUMMARY_PATH)
    population_summary = read_json(POPULATION_SUMMARY_PATH)
    combined_impact = read_json(COMBINED_IMPACT_PATH)
    require(BUILDINGS_PATH.is_file(), f"Affected building data is missing: {BUILDINGS_PATH}")
    require(POPULATION_PATH.is_file(), f"WorldPop raster is missing: {POPULATION_PATH}")
    require(combined_impact.get("event_id") == 1104121
            and combined_impact.get("episode_id") == 20
            and combined_impact.get("polygon_type") == "Poly_Affected",
            "Step 2D summary does not match the selected GDACS event, episode, and geometry.")
    require(combined_impact.get("no_synthetic_data") is True
            and combined_impact.get("no_poly_global_used") is True,
            "Step 2D summary does not rule out synthetic data and Poly_Global.")
    return affected_area, feature_properties, flood_metadata, building_summary, population_summary, combined_impact


def build_projected_grid(affected_projected, cell_size_m: int) -> dict:
    min_x, min_y, max_x, max_y = affected_projected.bounds
    origin_x = math.floor(min_x / cell_size_m) * cell_size_m
    origin_y = math.floor(min_y / cell_size_m) * cell_size_m
    columns = math.ceil((max_x - origin_x) / cell_size_m)
    rows = math.ceil((max_y - origin_y) / cell_size_m)
    grid_rows, grid_columns = np.indices((rows, columns))
    x_min = origin_x + grid_columns.ravel() * cell_size_m
    y_min = origin_y + grid_rows.ravel() * cell_size_m
    squares = shapely.box(
        x_min,
        y_min,
        x_min + cell_size_m,
        y_min + cell_size_m,
    )
    clipped = shapely.intersection(squares, affected_projected)
    areas = shapely.area(clipped)
    valid = np.flatnonzero((areas > 0) & ~shapely.is_empty(clipped))
    cell_rows = grid_rows.ravel()[valid]
    cell_columns = grid_columns.ravel()[valid]
    square_polygons = squares[valid]
    clipped_polygons = clipped[valid]
    require(len(valid) > 0, "No grid cells intersect the GDACS affected area.")
    require(bool(np.all(shapely.is_valid(clipped_polygons))),
            "Grid intersection produced invalid geometries.")
    return {
        "origin_x": origin_x,
        "origin_y": origin_y,
        "rows": rows,
        "columns": columns,
        "cell_rows": cell_rows,
        "cell_columns": cell_columns,
        "square_polygons": square_polygons,
        "clipped_polygons": clipped_polygons,
        "clipped_area_m2": areas[valid],
        "cell_size_m": cell_size_m,
        "index_by_row_column": {
            (int(row), int(column)): index
            for index, (row, column) in enumerate(zip(cell_rows, cell_columns))
        },
    }


def aggregate_population(
    grid: dict,
    affected_area,
    population_summary: dict,
) -> tuple[np.ndarray, np.ndarray]:
    population_by_cell = np.zeros(len(grid["cell_rows"]), dtype="float64")
    pixel_count_by_cell = np.zeros(len(grid["cell_rows"]), dtype="int64")
    cell_size = grid["cell_size_m"]
    index_by_row_column = grid["index_by_row_column"]
    with rasterio.open(POPULATION_PATH) as raster:
        require(raster.crs is not None and CRS.from_user_input(raster.crs).equals(
            CRS.from_epsg(4326), ignore_axis_order=True
        ), "WorldPop raster must use EPSG:4326.")
        require(raster.nodata is not None, "WorldPop raster NoData is not identified.")
        boundary_metadata = read_json(ROOT / "data/processed/population/bihar_population_metadata.json")
        require("people per pixel" in boundary_metadata.get("units", "").lower(),
                "WorldPop unit semantics must identify people per pixel.")
        require(raster.nodata == boundary_metadata.get("nodata"),
                "WorldPop raster NoData differs from validated metadata.")
        to_projected = Transformer.from_crs(raster.crs, ANALYSIS_CRS, always_xy=True)
        cols = np.arange(raster.width, dtype="float64") + 0.5
        xs = raster.transform.c + cols * raster.transform.a
        valid_pixel_count = 0
        population_total = 0.0
        affected_mask_geometry = [mapping(affected_area)]
        chunk_rows = 128
        for row_start in range(0, raster.height, chunk_rows):
            height = min(chunk_rows, raster.height - row_start)
            window = Window(0, row_start, raster.width, height)
            values = raster.read(1, window=window)
            chunk_transform = raster.window_transform(window)
            in_affected_area = geometry_mask(
                affected_mask_geometry,
                out_shape=values.shape,
                transform=chunk_transform,
                all_touched=False,
                invert=True,
            )
            valid_pixels = np.isfinite(values) & (values != raster.nodata) & in_affected_area
            if not bool(np.any(valid_pixels)):
                continue
            require(bool(np.all(values[valid_pixels] >= 0)),
                    "WorldPop contains negative valid population values.")
            row_values = np.arange(row_start, row_start + height, dtype="float64") + 0.5
            ys = raster.transform.f + row_values * raster.transform.e
            xx, yy = np.meshgrid(xs, ys)
            projected_x, projected_y = to_projected.transform(xx, yy)
            pixel_cols = np.floor((projected_x - grid["origin_x"]) / cell_size).astype("int64")
            pixel_rows = np.floor((projected_y - grid["origin_y"]) / cell_size).astype("int64")
            selected_rows, selected_cols = np.nonzero(valid_pixels)
            population_values = values[valid_pixels].astype("float64", copy=False)
            cell_indices = np.fromiter(
                (
                    index_by_row_column.get(
                        (int(pixel_rows[row, column]), int(pixel_cols[row, column])),
                        -1,
                    )
                    for row, column in zip(selected_rows, selected_cols)
                ),
                dtype="int64",
                count=len(selected_rows),
            )
            require(bool(np.all(cell_indices >= 0)),
                    "A valid affected-area population pixel did not map to a generated grid cell.")
            population_by_cell += np.bincount(
                cell_indices,
                weights=population_values,
                minlength=len(population_by_cell),
            )
            pixel_count_by_cell += np.bincount(
                cell_indices,
                minlength=len(pixel_count_by_cell),
            )
            valid_pixel_count += int(len(population_values))
            population_total += float(np.sum(population_values, dtype=np.float64))
    require(valid_pixel_count == population_summary.get("affected_pixels"),
            "Grid valid population pixel total differs from the Step 2D affected-pixel total.")
    require(math.isclose(
        population_total,
        population_summary.get("affected_population", math.nan),
        rel_tol=1e-10,
        abs_tol=0.01,
    ), "Grid population sum differs from the Step 2D affected population.")
    return population_by_cell, pixel_count_by_cell


def aggregate_buildings(grid: dict, building_summary: dict) -> tuple[np.ndarray, np.ndarray]:
    count_by_cell = np.zeros(len(grid["cell_rows"]), dtype="int64")
    area_by_cell_m2 = np.zeros(len(grid["cell_rows"]), dtype="float64")
    cell_size = grid["cell_size_m"]
    tree = STRtree(grid["square_polygons"])
    index_by_row_column = grid["index_by_row_column"]
    to_projected = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
    building_count = 0
    total_area_m2 = 0.0
    with open_arrow(BUILDINGS_PATH, batch_size=8192) as context:
        arrow_metadata, arrow_stream = context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        geometry_name = geometry_column_name(arrow_metadata, reader.schema)
        require("osm_id" in reader.schema.names, "Affected building output does not preserve OSM IDs.")
        for batch in reader:
            wkb = batch.column(geometry_name).to_pylist()
            require(all(value is not None for value in wkb),
                    "Affected buildings contain missing geometries.")
            geometries = shapely.from_wkb(wkb)
            require(bool(np.all(shapely.is_valid(geometries) & ~shapely.is_empty(geometries))),
                    "Affected buildings contain invalid or empty geometries.")
            require(bool(np.all(np.isin(shapely.get_type_id(geometries), [3, 6]))),
                    "Affected building input contains non-polygon geometries.")
            projected_geometries = shapely.transform(
                geometries, to_projected.transform, interleaved=False
            )
            building_areas = shapely.area(projected_geometries)
            total_area_m2 += float(np.sum(building_areas, dtype=np.float64))
            points = shapely.point_on_surface(projected_geometries)
            coordinates = shapely.get_coordinates(points)
            cell_cols = np.floor(
                (coordinates[:, 0] - grid["origin_x"]) / cell_size
            ).astype("int64")
            cell_rows = np.floor(
                (coordinates[:, 1] - grid["origin_y"]) / cell_size
            ).astype("int64")
            count_indices = np.fromiter(
                (
                    index_by_row_column.get((int(row), int(column)), -1)
                    for row, column in zip(cell_rows, cell_cols)
                ),
                dtype="int64",
                count=len(cell_rows),
            )
            require(bool(np.all(count_indices >= 0)),
                    "An affected OSM building did not map to a grid cell.")
            count_by_cell += np.bincount(
                count_indices, minlength=len(count_by_cell)
            )

            pairs = tree.query(projected_geometries, predicate="intersects")
            building_indices, cell_indices = pairs
            if len(building_indices):
                intersections = shapely.intersection(
                    projected_geometries[building_indices],
                    grid["square_polygons"][cell_indices],
                )
                intersection_areas = shapely.area(intersections)
                positive = intersection_areas > 0
                area_by_cell_m2 += np.bincount(
                    cell_indices[positive],
                    weights=intersection_areas[positive],
                    minlength=len(area_by_cell_m2),
                )
            building_count += len(geometries)
    require(building_count == building_summary.get("affected_count"),
            "Grid building count differs from the Step 2D affected-building count.")
    require(math.isclose(
        total_area_m2 / 1_000_000,
        building_summary.get("affected_footprint_area_km2", math.nan),
        rel_tol=1e-8,
        abs_tol=1e-7,
    ), "Grid building-footprint total differs from the Step 2D affected footprint area.")
    require(int(count_by_cell.sum()) == building_count,
            "Building assignment to grid cells lost or double-counted features.")
    require(math.isclose(
        float(area_by_cell_m2.sum()),
        total_area_m2,
        rel_tol=1e-8,
        abs_tol=1e-4,
    ), "Grid building-area aggregation lost or double-counted footprint area.")
    return count_by_cell, area_by_cell_m2


def projected_to_geojson(grid: dict) -> list:
    to_geographic = Transformer.from_crs(ANALYSIS_CRS, STORAGE_CRS, always_xy=True)
    geometries = shapely.transform(
        grid["clipped_polygons"], to_geographic.transform, interleaved=False
    )
    require(bool(np.all(shapely.is_valid(geometries) & ~shapely.is_empty(geometries))),
            "Transformed output grid contains invalid or empty geometries.")
    return list(geometries)


def acquire_disaster_id(session, properties: dict, affected_area) -> int:
    event_id = str(properties["event_id"])
    disaster = session.scalar(
        select(Disaster).where(
            Disaster.source == "GDACS",
            Disaster.external_id == event_id,
        )
    )
    if disaster is not None:
        return disaster.id
    start_time = properties.get("source_feature_properties", {}).get("fromdate")
    end_time = properties.get("source_feature_properties", {}).get("todate")
    disaster = Disaster(
        source="GDACS",
        external_id=event_id,
        disaster_type="flood",
        name=properties.get("source_feature_properties", {}).get("name") or "Flood in India",
        country=properties.get("source_feature_properties", {}).get("country") or "India",
        severity=properties.get("source_feature_properties", {}).get("alertlevel"),
        start_time=datetime.fromisoformat(start_time) if start_time else None,
        end_time=datetime.fromisoformat(end_time) if end_time else None,
        geometry=from_shape(affected_area, srid=4326),
        metadata_json=json.dumps(
            {
                "source": "GDACS",
                "event_id": int(event_id),
                "selected_episode_id": properties.get("episode_id"),
                "selected_polygon_type": properties.get("polygon_type"),
                "geometry_semantics": "GDACS analyst-reported affected area",
                "not_observed_inundation": True,
            }
        ),
    )
    session.add(disaster)
    session.flush()
    return disaster.id


def persist_zones(zone_rows: list[dict], properties: dict, affected_area) -> int:
    session = SessionLocal()
    try:
        with session.begin():
            disaster_id = acquire_disaster_id(session, properties, affected_area)
            session.execute(
                delete(ImpactZone).where(
                    ImpactZone.disaster_id == disaster_id,
                    ImpactZone.zone_code.like(f"{ZONE_CODE_PREFIX}%"),
                )
            )
            for row in zone_rows:
                session.add(
                    ImpactZone(
                        disaster_id=disaster_id,
                        zone_code=row["zone_id"],
                        geometry=from_shape(row["geometry"], srid=4326),
                        population_count=Decimal(str(row["population"])),
                        building_count=row["building_count"],
                        building_footprint_area_km2=Decimal(
                            str(row["building_footprint_area_km2"])
                        ),
                        hazard_coverage=Decimal(str(row["disaster_exposure"])),
                        population_exposure=Decimal(str(row["population_exposure"])),
                        building_exposure=Decimal(str(row["building_exposure"])),
                        infrastructure_exposure=None,
                        vulnerability_score=None,
                        impact_score=Decimal(str(row["impact_score"])),
                        severity_class=row["severity_class"],
                    )
                )
        return disaster_id
    finally:
        session.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell-size-m", type=int, default=10_000)
    parser.add_argument("--moderate-min", type=float, default=0.25)
    parser.add_argument("--high-min", type=float, default=0.50)
    parser.add_argument("--critical-min", type=float, default=0.75)
    parser.add_argument("--weight-disaster", type=float, default=0.40)
    parser.add_argument("--weight-population", type=float, default=0.25)
    parser.add_argument("--weight-buildings", type=float, default=0.15)
    parser.add_argument("--weight-infrastructure", type=float, default=0.10)
    parser.add_argument("--weight-vulnerability", type=float, default=0.10)
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        require(args.cell_size_m > 0, "Grid cell size must be positive.")
        thresholds = {
            "moderate_min": args.moderate_min,
            "high_min": args.high_min,
            "critical_min": args.critical_min,
        }
        weights = {
            "disaster_exposure": args.weight_disaster,
            "population_exposure": args.weight_population,
            "building_exposure": args.weight_buildings,
            "infrastructure_exposure": args.weight_infrastructure,
            "vulnerability": args.weight_vulnerability,
        }
        classify_severity(0.0, thresholds=thresholds)
        require(all(math.isfinite(value) and value >= 0 for value in weights.values())
                and sum(weights.values()) > 0,
                "Weights must be finite, non-negative, and not all zero.")

        affected_area, flood_properties, flood_metadata, building_summary, population_summary, impact_summary = (
            validate_inputs()
        )
        to_projected = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
        affected_projected = shapely.transform(
            affected_area, to_projected.transform, interleaved=False
        )
        require(affected_projected.is_valid and not affected_projected.is_empty,
                "Projected GDACS affected area is invalid or empty.")
        grid = build_projected_grid(affected_projected, args.cell_size_m)
        population_by_cell, population_pixel_counts = aggregate_population(
            grid, affected_area, population_summary
        )
        building_counts, building_areas_m2 = aggregate_buildings(grid, building_summary)
        geometries_4326 = projected_to_geojson(grid)

        area_coverage = grid["clipped_area_m2"] / float(args.cell_size_m**2)
        population_exposure = normalize_by_maximum(population_by_cell.tolist())
        building_exposure = normalize_by_maximum(building_counts.astype("float64").tolist())
        scores = [
            calculate_baseline_score(
                {
                    "disaster_exposure": float(area_coverage[index]),
                    "population_exposure": population_exposure[index],
                    "building_exposure": building_exposure[index],
                    "infrastructure_exposure": None,
                    "vulnerability": None,
                },
                weights=weights,
            )
            for index in range(len(geometries_4326))
        ]
        classes = [
            classify_severity(score, thresholds=thresholds)
            for score in scores
        ]
        disaster_reference = f"GDACS:{flood_properties['event_id']}"
        zone_rows: list[dict] = []
        for index, geometry in enumerate(geometries_4326):
            row_number = int(grid["cell_rows"][index])
            column_number = int(grid["cell_columns"][index])
            zone_id = (
                f"{ZONE_CODE_PREFIX}{args.cell_size_m}M-"
                f"R{row_number:04d}-C{column_number:04d}"
            )
            zone_rows.append(
                {
                    "zone_id": zone_id,
                    "disaster_id": None,
                    "disaster_external_id": disaster_reference,
                    "hazard_semantics": "GDACS reported affected area",
                    "not_observed_inundation": True,
                    "grid_row": row_number,
                    "grid_column": column_number,
                    "geometry": geometry,
                    "population": float(population_by_cell[index]),
                    "population_valid_pixel_count": int(population_pixel_counts[index]),
                    "building_count": int(building_counts[index]),
                    "building_footprint_area_km2": float(building_areas_m2[index] / 1_000_000),
                    "disaster_exposure": float(area_coverage[index]),
                    "population_exposure": float(population_exposure[index]),
                    "building_exposure": float(building_exposure[index]),
                    "infrastructure_exposure": None,
                    "vulnerability_score": None,
                    "impact_score": float(scores[index]),
                    "severity_class": classes[index],
                }
            )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        counts = Counter(classes)
        total_grid_area = float(np.sum(grid["clipped_area_m2"], dtype=np.float64))
        affected_area_m2 = float(shapely.area(affected_projected))
        require(math.isclose(total_grid_area, affected_area_m2, rel_tol=1e-10, abs_tol=0.01),
                "Generated grid coverage area differs from the projected affected area.")
        disaster_id = persist_zones(zone_rows, flood_properties, affected_area)
        for row in zone_rows:
            row["disaster_id"] = disaster_id

        document = {
            "type": "FeatureCollection",
            "name": "GDACS affected-area impact grid",
            "coordinate_reference_system": STORAGE_CRS,
            "features": [
                {
                    "type": "Feature",
                    "id": row["zone_id"],
                    "properties": {
                        key: value
                        for key, value in row.items()
                        if key != "geometry"
                    },
                    "geometry": mapping(row["geometry"]),
                }
                for row in zone_rows
            ],
        }
        GRID_PATH.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

        available_weight_sum = sum(
            weights[name]
            for name in (
                "disaster_exposure",
                "population_exposure",
                "building_exposure",
            )
        )
        score_min = min(scores)
        score_max = max(scores)
        summary = {
            "event_id": flood_properties["event_id"],
            "episode_id": flood_properties["episode_id"],
            "polygon_type": flood_properties["polygon_type"],
            "disaster_id": disaster_id,
            "hazard_semantics": "GDACS reported affected area",
            "not_observed_inundation": True,
            "cell_count": len(zone_rows),
            "score_range": {"minimum": score_min, "maximum": score_max},
            "severity_counts": {
                severity: counts.get(severity, 0)
                for severity in ("Low", "Moderate", "High", "Critical")
            },
            "totals": {
                "population": float(np.sum(population_by_cell, dtype=np.float64)),
                "population_valid_pixel_count": int(np.sum(population_pixel_counts)),
                "building_count": int(np.sum(building_counts)),
                "building_footprint_area_km2": float(np.sum(building_areas_m2) / 1_000_000),
                "affected_area_km2": affected_area_m2 / 1_000_000,
                "grid_area_km2": total_grid_area / 1_000_000,
            },
            "outputs": {
                "grid": GRID_PATH.name,
                "summary": SUMMARY_PATH.name,
                "metadata": METADATA_PATH.name,
            },
        }
        metadata = {
            "event_id": flood_properties["event_id"],
            "episode_id": flood_properties["episode_id"],
            "polygon_type": flood_properties["polygon_type"],
            "disaster_id": disaster_id,
            "hazard_semantics": "GDACS analyst-reported affected area; not observed inundation",
            "source_file": str(FLOOD_PATH.relative_to(ROOT)),
            "step1_flood_metadata": str(FLOOD_METADATA_PATH.relative_to(ROOT)),
            "source_geometry_url": flood_properties.get("source_url"),
            "step2d_inputs": {
                "buildings": str(BUILDINGS_PATH.relative_to(ROOT)),
                "building_summary": str(BUILDING_SUMMARY_PATH.relative_to(ROOT)),
                "population": str(POPULATION_PATH.relative_to(ROOT)),
                "population_summary": str(POPULATION_SUMMARY_PATH.relative_to(ROOT)),
                "combined_impact_summary": str(COMBINED_IMPACT_PATH.relative_to(ROOT)),
            },
            "grid": {
                "cell_size_m": args.cell_size_m,
                "analysis_crs": ANALYSIS_CRS,
                "storage_crs": STORAGE_CRS,
                "alignment": "Grid origin aligned to multiples of cell_size_m in EPSG:32645.",
                "row_count": grid["rows"],
                "column_count": grid["columns"],
                "origin_x_m": grid["origin_x"],
                "origin_y_m": grid["origin_y"],
                "clipping": "Each projected square cell is intersected with the selected GDACS affected area; only positive-area intersections are retained.",
            },
            "features": {
                "available": [
                    "disaster_exposure (clipped hazard coverage / full square-cell area)",
                    "population_exposure (WorldPop people-per-pixel sum normalized by the maximum grid-cell sum)",
                    "building_exposure (unique affected OSM footprints normalized by the maximum grid-cell count)",
                    "affected_population (estimated people)",
                    "population_valid_pixel_count",
                    "affected_building_count",
                    "affected_building_footprint_area_km2",
                ],
                "unavailable": [
                    "infrastructure_exposure: no approved infrastructure dataset is available in Step 1/2 inputs",
                    "vulnerability: no approved vulnerability dataset is available for this study area",
                ],
                "population_assignment": "Every valid WorldPop pixel whose center lies in the GDACS affected area is assigned once to its containing projected grid cell. Source values are estimated people per pixel, not people per km2.",
                "building_assignment": "Each unique Step 2D clipped OSM building feature is assigned once by an interior point; its clipped footprint area is partitioned across intersected projected grid squares.",
                "normalization": "Population and building exposures are divided by their respective observed maximum cell values; zero-valued populations/counts normalize to zero when the maximum is zero. Disaster exposure is the clipped-area fraction, bounded to [0,1].",
            },
            "score": {
                "kind": "configurable engineering baseline; weights are not scientifically validated",
                "formula": "weighted sum of available normalized features divided by the sum of their configured weights",
                "configured_weights": weights,
                "available_weight_sum": available_weight_sum,
                "effective_weights": {
                    "disaster_exposure": weights["disaster_exposure"] / available_weight_sum,
                    "population_exposure": weights["population_exposure"] / available_weight_sum,
                    "building_exposure": weights["building_exposure"] / available_weight_sum,
                },
                "excluded_features": [
                    "infrastructure_exposure",
                    "vulnerability",
                ],
                "not_a_validated_domain_model": True,
            },
            "severity_thresholds": {
                "Low": f"score < {thresholds['moderate_min']}",
                "Moderate": f"{thresholds['moderate_min']} <= score < {thresholds['high_min']}",
                "High": f"{thresholds['high_min']} <= score < {thresholds['critical_min']}",
                "Critical": f"score >= {thresholds['critical_min']}",
                "configurable_values": thresholds,
            },
            "validation_inputs": {
                "step2d_affected_population": population_summary["affected_population"],
                "step2d_affected_pixels": population_summary["affected_pixels"],
                "step2d_affected_buildings": building_summary["affected_count"],
                "step2d_affected_building_area_km2": building_summary["affected_footprint_area_km2"],
                "step2d_summary_confirmed_no_synthetic_data": impact_summary["no_synthetic_data"],
                "step2d_summary_confirmed_no_poly_global": impact_summary["no_poly_global_used"],
            },
            "limitations": [
                "The GDACS polygon is an analyst-reported affected area, not an observed satellite-derived inundation extent.",
                "The score is an engineering baseline using configurable weights, not scientifically validated domain weights.",
                "Infrastructure and approved vulnerability data are unavailable and therefore omitted, not fabricated.",
                "Population is a modelled WorldPop estimate in people per grid pixel; NoData cells are excluded.",
                "OSM building completeness is variable and footprints are not a complete building census.",
                "The grid is a geographic summarization of reported-area exposure, not a prediction of damage or actual flooding.",
            ],
        }
        SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        METADATA_PATH.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(json.dumps({"summary": summary, "metadata": str(METADATA_PATH.relative_to(ROOT))}, indent=2))
        return 0
    except Exception as error:
        print(f"GRID GENERATION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
