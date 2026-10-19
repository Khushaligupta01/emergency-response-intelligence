"""Independently validate the projected-grid impact and severity outputs."""

import json
import math
import os
from collections import Counter
from pathlib import Path
import sys

import pyproj

os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import numpy as np
import rasterio
import shapely
from pyproj import CRS, Transformer
from shapely.geometry import shape
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.disaster import Disaster
from app.models.impact_zone import ImpactZone
from app.services.zone_severity import (
    calculate_baseline_score,
    classify_severity,
)

FLOOD_PATH = ROOT / "data/processed/gdacs/FL_1104121/bihar_flood_extent.geojson"
BUILDING_SUMMARY_PATH = ROOT / "data/processed/impact/building_impact_summary.json"
POPULATION_SUMMARY_PATH = ROOT / "data/processed/impact/population_impact_summary.json"
COMBINED_IMPACT_PATH = ROOT / "data/processed/impact/impact_assessment_summary.json"
GRID_PATH = ROOT / "data/processed/zones/disaster_grid.geojson"
SUMMARY_PATH = ROOT / "data/processed/zones/zone_severity_summary.json"
METADATA_PATH = ROOT / "data/processed/zones/zone_generation_metadata.json"
POPULATION_PATH = ROOT / "data/processed/population/bihar_population.tif"
ANALYSIS_CRS = "EPSG:32645"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    require(path.is_file(), f"Required file is missing: {path}")
    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Invalid JSON at {path}: {error}") from error
    require(isinstance(value, dict), f"Expected a JSON object at {path}.")
    return value


def main() -> int:
    try:
        flood_document = read_json(FLOOD_PATH)
        require(flood_document.get("type") == "FeatureCollection",
                "GDACS source is not a FeatureCollection.")
        require(flood_document.get("coordinate_reference_system") == "EPSG:4326",
                "GDACS source CRS is not EPSG:4326.")
        flood_features = flood_document.get("features")
        require(isinstance(flood_features, list) and flood_features,
                "GDACS affected-area geometry is empty.")
        hazard = shapely.union_all([
            shape(feature["geometry"]) for feature in flood_features
        ])
        require(hazard.is_valid and not hazard.is_empty, "GDACS affected-area geometry is invalid.")

        building_summary = read_json(BUILDING_SUMMARY_PATH)
        population_summary = read_json(POPULATION_SUMMARY_PATH)
        impact_summary = read_json(COMBINED_IMPACT_PATH)
        summary = read_json(SUMMARY_PATH)
        metadata = read_json(METADATA_PATH)
        require(impact_summary.get("event_id") == 1104121
                and impact_summary.get("episode_id") == 20
                and impact_summary.get("polygon_type") == "Poly_Affected",
                "Step 2D inputs do not refer to the selected GDACS event, episode, and polygon.")
        require(impact_summary.get("hazard_semantics") == "GDACS reported affected area"
                and impact_summary.get("not_observed_inundation") is True,
                "Step 2D hazard semantics are missing or incorrect.")
        require(impact_summary.get("no_synthetic_data") is True
                and impact_summary.get("no_poly_global_used") is True,
                "Step 2D summary does not exclude synthetic data and Poly_Global.")

        document = read_json(GRID_PATH)
        require(document.get("type") == "FeatureCollection", "Grid output is not a FeatureCollection.")
        require(document.get("coordinate_reference_system") == "EPSG:4326",
                "Grid output is not declared as EPSG:4326.")
        features = document.get("features")
        require(isinstance(features, list) and features, "Grid output has no cells.")
        require(len(features) == summary.get("cell_count"), "Grid feature count differs from summary.")
        properties = [feature.get("properties", {}) for feature in features]
        geometries_4326 = [shape(feature["geometry"]) for feature in features]
        require(all(geometry.is_valid and not geometry.is_empty for geometry in geometries_4326),
                "Grid output contains invalid or empty geometries.")
        require(all(geometry.geom_type in ("Polygon", "MultiPolygon") for geometry in geometries_4326),
                "Grid output contains non-polygonal geometry.")
        zone_ids = [item.get("zone_id") for item in properties]
        require(all(isinstance(zone_id, str) and zone_id for zone_id in zone_ids),
                "Grid output has a missing zone_id.")
        require(len(set(zone_ids)) == len(zone_ids), "Grid zone_id values are not unique.")
        disaster_ids = {item.get("disaster_id") for item in properties}
        require(len(disaster_ids) == 1 and next(iter(disaster_ids)) == summary.get("disaster_id"),
                "Grid cells do not refer to one persisted disaster_id.")
        require(all(item.get("disaster_external_id") == "GDACS:1104121" for item in properties),
                "Grid cells do not identify GDACS event 1104121.")

        to_projected = Transformer.from_crs("EPSG:4326", ANALYSIS_CRS, always_xy=True)
        hazard_projected = shapely.transform(hazard, to_projected.transform, interleaved=False)
        geometries_projected = shapely.transform(
            np.asarray(geometries_4326, dtype=object),
            to_projected.transform,
            interleaved=False,
        )
        require(bool(np.all(shapely.is_valid(geometries_projected))),
                "Projected grid contains invalid cells.")
        outside_areas = []
        for geometry in geometries_projected:
            outside = shapely.difference(geometry, hazard_projected)
            outside_areas.append(float(shapely.area(outside)))
        require(max(outside_areas, default=0.0) <= 1e-4,
                "A grid cell extends materially outside the affected area.")
        grid_union = shapely.union_all(geometries_projected)
        symmetric_difference_area = float(
            shapely.area(shapely.symmetric_difference(grid_union, hazard_projected))
        )
        hazard_area_m2 = float(shapely.area(hazard_projected))
        require(symmetric_difference_area <= max(0.01, hazard_area_m2 * 1e-10),
                "Grid cells leave a gap or omit area from the affected area.")
        cell_areas = shapely.area(geometries_projected)
        grid_area_m2 = float(np.sum(cell_areas, dtype=np.float64))
        require(math.isclose(grid_area_m2, hazard_area_m2, rel_tol=1e-10, abs_tol=0.01),
                "Summed grid cell area does not cover the affected area exactly.")

        tree = STRtree(geometries_projected)
        overlap_pairs = tree.query(geometries_projected, predicate="intersects")
        first_indices, second_indices = overlap_pairs
        unique_pairs = first_indices < second_indices
        overlap_areas = shapely.area(
            shapely.intersection(
                geometries_projected[first_indices[unique_pairs]],
                geometries_projected[second_indices[unique_pairs]],
            )
        )
        require(float(np.sum(overlap_areas, dtype=np.float64)) <= 1e-4,
                "Grid cells have overlapping positive-area regions.")

        cell_size = metadata.get("grid", {}).get("cell_size_m")
        require(isinstance(cell_size, int) and cell_size > 0,
                "Projected grid cell size is invalid.")
        require(metadata.get("grid", {}).get("analysis_crs") == ANALYSIS_CRS,
                "Grid generation metadata does not use the expected projected CRS.")
        require(metadata.get("grid", {}).get("storage_crs") == "EPSG:4326",
                "Grid generation metadata does not declare EPSG:4326 storage.")
        for row, item in zip(features, properties):
            require(row.get("id") == item.get("zone_id"), "GeoJSON feature id differs from zone_id.")
            require(item.get("hazard_semantics") == "GDACS reported affected area"
                    and item.get("not_observed_inundation") is True,
                    "Grid cell has unexpected hazard semantics.")
            require(
                isinstance(item.get("grid_row"), int)
                and isinstance(item.get("grid_column"), int)
                and item["grid_row"] >= 0
                and item["grid_column"] >= 0,
                "Grid row/column coordinates are missing or invalid.",
            )
            for key in (
                "population",
                "population_valid_pixel_count",
                "building_count",
                "building_footprint_area_km2",
                "disaster_exposure",
                "population_exposure",
                "building_exposure",
                "impact_score",
            ):
                require(key in item and item[key] is not None,
                        f"Grid cell lacks required metric {key}.")
            require(item["population"] >= 0 and item["population_valid_pixel_count"] >= 0,
                    "Grid cell has negative population or pixel count.")
            require(item["building_count"] >= 0 and item["building_footprint_area_km2"] >= 0,
                    "Grid cell has negative building metrics.")
            for key in ("disaster_exposure", "population_exposure", "building_exposure", "impact_score"):
                require(math.isfinite(item[key]) and 0 <= item[key] <= 1,
                        f"Grid cell {key} is outside [0, 1].")
            require(item.get("infrastructure_exposure") is None
                    and item.get("vulnerability_score") is None,
                    "Unavailable infrastructure or vulnerability exposure was fabricated.")
        grid_origin_x = metadata.get("grid", {}).get("origin_x_m")
        grid_origin_y = metadata.get("grid", {}).get("origin_y_m")
        require(isinstance(grid_origin_x, (int, float)) and isinstance(grid_origin_y, (int, float)),
                "Grid origin is missing from generation metadata.")
        expected_squares = shapely.box(
            grid_origin_x + np.asarray([item["grid_column"] for item in properties]) * cell_size,
            grid_origin_y + np.asarray([item["grid_row"] for item in properties]) * cell_size,
            grid_origin_x + (np.asarray([item["grid_column"] for item in properties]) + 1) * cell_size,
            grid_origin_y + (np.asarray([item["grid_row"] for item in properties]) + 1) * cell_size,
        )
        outside_squares = shapely.area(
            shapely.difference(geometries_projected, expected_squares)
        )
        require(float(np.max(outside_squares, initial=0.0)) <= 1e-4,
                "A grid cell extends outside its assigned regular square.")
        expected_hazard_coverage = cell_areas / float(cell_size**2)
        recorded_hazard_coverage = np.asarray(
            [item["disaster_exposure"] for item in properties],
            dtype="float64",
        )
        require(bool(np.allclose(
            expected_hazard_coverage,
            recorded_hazard_coverage,
            rtol=1e-9,
            atol=1e-9,
        )), "Hazard coverage does not match projected clipped area divided by square-cell area.")
        weights = metadata.get("score", {}).get("configured_weights", {})
        thresholds = metadata.get("severity_thresholds", {}).get("configurable_values", {})
        population_values = [item["population"] for item in properties]
        building_values = [item["building_count"] for item in properties]
        maximum_population = max(population_values)
        maximum_buildings = max(building_values)
        severity_counts = Counter()
        scores = []
        for item in properties:
            expected_population_exposure = (
                item["population"] / maximum_population if maximum_population else 0.0
            )
            expected_building_exposure = (
                item["building_count"] / maximum_buildings if maximum_buildings else 0.0
            )
            require(math.isclose(item["population_exposure"], expected_population_exposure,
                                 rel_tol=1e-9, abs_tol=1e-9),
                    "Population exposure does not match documented max normalization.")
            require(math.isclose(item["building_exposure"], expected_building_exposure,
                                 rel_tol=1e-9, abs_tol=1e-9),
                    "Building exposure does not match documented max normalization.")
            expected_score = calculate_baseline_score(
                {
                    "disaster_exposure": item["disaster_exposure"],
                    "population_exposure": item["population_exposure"],
                    "building_exposure": item["building_exposure"],
                    "infrastructure_exposure": None,
                    "vulnerability": None,
                },
                weights=weights,
            )
            require(math.isclose(item["impact_score"], expected_score,
                                 rel_tol=1e-8, abs_tol=1e-8),
                    "Grid impact score differs from the documented baseline formula.")
            expected_class = classify_severity(expected_score, thresholds=thresholds)
            require(item.get("severity_class") == expected_class,
                    "Severity class does not match the configured score thresholds.")
            scores.append(expected_score)
            severity_counts[expected_class] += 1

        total_population = float(sum(population_values))
        total_pixels = int(sum(item["population_valid_pixel_count"] for item in properties))
        total_buildings = int(sum(item["building_count"] for item in properties))
        total_building_area = float(sum(item["building_footprint_area_km2"] for item in properties))
        require(total_population >= 0 and math.isclose(
            total_population,
            population_summary["affected_population"],
            rel_tol=1e-10,
            abs_tol=0.01,
        ), "Grid population sum differs from Step 2D.")
        require(total_pixels == population_summary["affected_pixels"],
                "Grid population pixel count differs from Step 2D.")
        require(total_buildings == building_summary["affected_count"],
                "Grid building count differs from Step 2D.")
        require(math.isclose(
            total_building_area,
            building_summary["affected_footprint_area_km2"],
            rel_tol=1e-8,
            abs_tol=1e-6,
        ), "Grid footprint area differs from Step 2D.")
        require(total_buildings == impact_summary["buildings"]["affected_count"],
                "Grid buildings do not match the combined Step 2D summary.")
        require(total_pixels == impact_summary["population"]["affected_pixels"],
                "Grid pixels do not match the combined Step 2D summary.")
        require(math.isclose(
            total_population,
            summary["totals"]["population"],
            rel_tol=1e-12,
            abs_tol=0.01,
        ) and total_pixels == summary["totals"]["population_valid_pixel_count"]
                and total_buildings == summary["totals"]["building_count"],
                "Grid totals differ from the zone summary.")
        expected_severity_counts = {
            severity: severity_counts.get(severity, 0)
            for severity in ("Low", "Moderate", "High", "Critical")
        }
        require(expected_severity_counts == summary.get("severity_counts"),
                "Severity class counts differ from the zone summary.")
        require(math.isclose(min(scores), summary["score_range"]["minimum"], abs_tol=1e-10)
                and math.isclose(max(scores), summary["score_range"]["maximum"], abs_tol=1e-10),
                "Score range differs from the zone summary.")
        require(metadata.get("score", {}).get("not_a_validated_domain_model") is True,
                "Engineering baseline limitations are not documented.")
        require(metadata.get("features", {}).get("unavailable")
                and metadata.get("score", {}).get("excluded_features")
                == ["infrastructure_exposure", "vulnerability"],
                "Unavailable features are not documented as excluded.")

        with rasterio.open(POPULATION_PATH) as raster:
            require(raster.crs is not None and CRS.from_user_input(raster.crs).equals(
                CRS.from_epsg(4326), ignore_axis_order=True
            ), "Population raster CRS is invalid.")
            require(raster.nodata is not None, "Population raster NoData is not handled.")
            require("people per pixel" in read_json(
                ROOT / "data/processed/population/bihar_population_metadata.json"
            ).get("units", "").lower(), "Population unit semantics are missing.")

        session = SessionLocal()
        try:
            disaster = session.scalar(
                select(Disaster).where(
                    Disaster.id == summary.get("disaster_id"),
                    Disaster.source == "GDACS",
                    Disaster.external_id == "1104121",
                )
            )
            require(disaster is not None, "Persisted GDACS disaster reference is missing.")
            zones = session.scalars(
                select(ImpactZone).where(
                    ImpactZone.disaster_id == summary["disaster_id"],
                    ImpactZone.zone_code.like("GDACS-1104121-GRID-%"),
                )
            ).all()
            require(len(zones) == len(features), "Persisted PostGIS zone count differs from GeoJSON.")
            require({zone.zone_code for zone in zones} == set(zone_ids),
                    "Persisted zone identifiers differ from GeoJSON.")
        finally:
            session.close()

        print(json.dumps({
            "status": "validated",
            "cell_count": len(features),
            "score_range": {"minimum": min(scores), "maximum": max(scores)},
            "severity_counts": expected_severity_counts,
            "totals_match_step2d": True,
            "grid_has_no_positive_area_gaps_or_overlaps": True,
            "database_zone_count": len(zones),
            "disaster_id": summary["disaster_id"],
        }, indent=2))
        return 0
    except Exception as error:
        print(f"GRID VALIDATION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
