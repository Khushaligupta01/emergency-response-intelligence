"""Independently validate the prepared Bihar OSM and WorldPop datasets."""

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


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_PATH = ROOT / "data/external/boundaries/bihar_boundary.geojson"
BUILDINGS_PATH = ROOT / "data/processed/osm/bihar_buildings.geojson"
BUILDINGS_METADATA_PATH = ROOT / "data/processed/osm/bihar_buildings_metadata.json"
POPULATION_PATH = ROOT / "data/processed/population/bihar_population.tif"
POPULATION_METADATA_PATH = ROOT / "data/processed/population/bihar_population_metadata.json"
SUMMARY_PATH = ROOT / "data/processed/step2c_data_summary.json"
AREA_CRS = "EPSG:32645"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    require(path.is_file(), f"Required JSON file is missing: {path}")
    try:
        with path.open(encoding="utf-8") as source:
            return json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Cannot read valid JSON from {path}: {error}") from error


def load_boundary():
    document = read_json(BOUNDARY_PATH)
    require(document.get("type") == "FeatureCollection", "Bihar boundary is not a FeatureCollection.")
    features = document.get("features")
    require(isinstance(features, list) and len(features) == 1, "Expected one Bihar boundary feature.")
    boundary_crs = document.get("crs", {}).get("properties", {}).get("name")
    require(
        boundary_crs in (None, "EPSG:4326", "urn:ogc:def:crs:OGC:1.3:CRS84"),
        f"Unexpected Bihar boundary CRS: {boundary_crs}",
    )
    boundary = shape(features[0]["geometry"])
    require(boundary.is_valid and not boundary.is_empty, "Bihar boundary is invalid or empty.")
    return boundary


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
    require(
        len(binary_columns) == 1,
        f"Cannot identify the GeoJSON WKB column in Arrow schema {schema}.",
    )
    return binary_columns[0]


def validate_buildings(boundary) -> dict:
    require(BUILDINGS_PATH.is_file(), f"Building GeoJSON is missing: {BUILDINGS_PATH}")
    metadata = read_json(BUILDINGS_METADATA_PATH)
    require(metadata.get("synthetic_generation_used") is False, "Building metadata does not explicitly rule out synthetic data.")
    require(
        "OpenStreetMap" in str(metadata.get("source", ""))
        and "Geofabrik" in str(metadata.get("source", "")),
        "Building metadata does not identify a real OSM-derived Geofabrik source.",
    )
    require(metadata.get("source_url", "").startswith("https://download.geofabrik.de/"), "Building source URL is missing or not Geofabrik.")
    require(metadata.get("crs") == "EPSG:4326", "Building metadata does not record EPSG:4326.")
    require(metadata.get("source_snapshot_date"), "Building source snapshot date is missing.")
    require(metadata.get("license") == "Open Database License (ODbL) 1.0", "OSM license information is missing.")

    with BUILDINGS_PATH.open("r", encoding="utf-8") as source:
        header = source.read(4096)
    require('"type":"FeatureCollection"' in header.replace(" ", ""), "Building output is not a GeoJSON FeatureCollection.")

    info = pyogrio.read_info(BUILDINGS_PATH)
    require(info.get("driver") == "GeoJSON", f"Building output is not recognized as GeoJSON: {info.get('driver')}")
    require(CRS.from_user_input(info.get("crs")).to_epsg() == 4326, f"Building output CRS is not EPSG:4326: {info.get('crs')}")
    require(
        info.get("geometry_type") in ("Polygon", "MultiPolygon", "Unknown"),
        f"Building layer is not polygonal: {info.get('geometry_type')}",
    )

    transformer = Transformer.from_crs("EPSG:4326", AREA_CRS, always_xy=True)
    feature_count = valid_count = invalid_count = repaired_count = 0
    area_m2 = 0.0
    bounds_min = np.array([math.inf, math.inf], dtype="float64")
    bounds_max = np.array([-math.inf, -math.inf], dtype="float64")
    any_intersection = False

    with open_arrow(BUILDINGS_PATH, batch_size=8192) as arrow_context:
        stream_metadata, arrow_stream = arrow_context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        geometry_name = geometry_column_name(stream_metadata, reader.schema)
        field_names = set(reader.schema.names)
        require("osm_id" in field_names, "Building GeoJSON does not preserve OSM IDs.")
        repaired_ids = {
            str(identifier)
            for identifier in (
                metadata.get("source_invalid_osm_ids_with_clipped_output", [])
                + metadata.get("clip_repaired_osm_ids", [])
            )
        }
        for batch in reader:
            geometries = shapely.from_wkb(batch.column(geometry_name).to_pylist())
            feature_count += len(geometries)
            valid = shapely.is_valid(geometries)
            empty = shapely.is_empty(geometries)
            types = shapely.get_type_id(geometries)
            polygonal = np.isin(types, [3, 6])
            valid_count += int(np.count_nonzero(valid & ~empty & polygonal))
            invalid_count += int(np.count_nonzero(~valid | empty | ~polygonal))
            require(
                bool(np.all(valid & ~empty & polygonal)),
                "Building output contains an invalid, empty, or non-polygonal geometry.",
            )
            require(
                bool(np.all(shapely.covers(boundary, geometries))),
                "At least one building geometry is not contained within Bihar.",
            )
            any_intersection = any_intersection or bool(np.any(shapely.intersects(boundary, geometries)))
            repaired_count += sum(
                str(identifier) in repaired_ids
                for identifier in batch.column("osm_id").to_pylist()
            )
            projected = shapely.transform(geometries, transformer.transform, interleaved=False)
            area_m2 += float(np.sum(shapely.area(projected)))
            batch_bounds = shapely.bounds(geometries)
            if len(batch_bounds):
                bounds_min = np.minimum(bounds_min, np.min(batch_bounds[:, :2], axis=0))
                bounds_max = np.maximum(bounds_max, np.max(batch_bounds[:, 2:], axis=0))

    require(feature_count > 0, "Building GeoJSON contains no features.")
    require(any_intersection, "Building output has no spatial intersection with Bihar.")
    require(area_m2 > 0, "Building footprint area is not positive.")
    require(feature_count == metadata.get("feature_count"), "Building count differs from metadata.")
    require(valid_count == metadata.get("valid_building_features"), "Valid building count differs from metadata.")
    require(invalid_count == metadata.get("invalid_building_features") == 0, "Invalid building count is nonzero or differs from metadata.")
    require(repaired_count == metadata.get("repaired_building_features"), "Repaired building count differs from metadata.")
    require(
        math.isclose(area_m2 / 1_000_000, metadata.get("total_building_footprint_area_km2", math.nan), rel_tol=1e-7),
        "Building area differs from metadata.",
    )
    return {
        "feature_count": feature_count,
        "valid_feature_count": valid_count,
        "invalid_feature_count": invalid_count,
        "repaired_feature_count": repaired_count,
        "crs": "EPSG:4326",
        "area_km2": area_m2 / 1_000_000,
        "bounds_epsg4326": [
            float(bounds_min[0]),
            float(bounds_min[1]),
            float(bounds_max[0]),
            float(bounds_max[1]),
        ],
        "intersects_bihar": any_intersection,
        "contained_in_bihar": True,
        "metadata": metadata,
    }


def validate_population(boundary) -> dict:
    require(POPULATION_PATH.is_file(), f"Population GeoTIFF is missing: {POPULATION_PATH}")
    metadata = read_json(POPULATION_METADATA_PATH)
    require(metadata.get("source") == "WorldPop, University of Southampton", "Population metadata does not identify WorldPop.")
    require(metadata.get("year") == 2020, "Population reference year is missing or unexpected.")
    require(metadata.get("catalog_record_region") == "India", "Population metadata does not identify the India catalog record.")
    require(metadata.get("documentation_url") == "https://hub.worldpop.org/geodata/summary?id=49804", "India-specific WorldPop documentation URL is missing.")
    require("people per pixel" in str(metadata.get("units", "")).lower(), "Population units are not documented as people per pixel.")
    require("not people per km" in str(metadata.get("raster_value_meaning", "")).lower(), "Population value semantics do not rule out people per km².")
    require(metadata.get("nodata") is not None, "Population NoData value is not identified.")
    require("license" in metadata and metadata.get("license_url"), "Population license/usage information is missing.")
    require(metadata.get("source_file"), "Raw WorldPop source file is not recorded.")
    require((ROOT / metadata["source_file"]).is_file(), "Raw WorldPop source file is missing.")

    with rasterio.open(POPULATION_PATH) as raster:
        require(raster.driver == "GTiff", f"Population output is not GeoTIFF: {raster.driver}")
        require(raster.count == 1, f"Unexpected population band count: {raster.count}")
        require(raster.crs is not None, "Population raster has no CRS.")
        require(
            CRS.from_user_input(raster.crs).equals(CRS.from_epsg(4326), ignore_axis_order=True),
            f"Population raster CRS is not EPSG:4326: {raster.crs}",
        )
        require(raster.nodata is not None, "Population raster has no NoData value.")
        require(raster.nodata == metadata["nodata"], "Population NoData differs from metadata.")
        require(raster.dtypes[0] == metadata.get("dtype"), "Population dtype differs from metadata.")
        require(
            "people per pixel" in raster.tags().get("units", "").lower(),
            "Population raster does not carry people-per-pixel unit tags.",
        )
        require(
            "worldpop" in raster.tags().get("source", "").lower(),
            "Population raster does not carry WorldPop source tags.",
        )
        resolution = [abs(raster.transform.a), abs(raster.transform.e)]
        documented_resolution = metadata.get("resolution", {}).get("degrees", [])
        require(
            len(documented_resolution) == 2
            and all(math.isclose(a, b, rel_tol=0, abs_tol=1e-12) for a, b in zip(resolution, documented_resolution)),
            "Population raster resolution differs from metadata.",
        )
        require(
            raster.width == metadata.get("width") and raster.height == metadata.get("height"),
            "Population raster dimensions differ from metadata.",
        )
        bounds = raster.bounds
        require(
            bounds.left <= boundary.bounds[0]
            and bounds.bottom <= boundary.bounds[1]
            and bounds.right >= boundary.bounds[2]
            and bounds.top >= boundary.bounds[3],
            "Population raster bounding box does not cover Bihar's bounding box.",
        )
        valid_count = boundary_pixel_count = 0
        minimum = math.inf
        maximum = -math.inf
        mask_geometry = [mapping(boundary)]
        chunk_rows = 128
        for row_start in range(0, raster.height, chunk_rows):
            rows = min(chunk_rows, raster.height - row_start)
            window = Window(0, row_start, raster.width, rows)
            values = raster.read(1, window=window)
            inside = geometry_mask(
                mask_geometry,
                out_shape=values.shape,
                transform=raster.window_transform(window),
                all_touched=False,
                invert=True,
            )
            valid = np.isfinite(values) & (values != raster.nodata)
            require(
                not bool(np.any(valid & ~inside)),
                "Population raster contains valid pixels outside the Bihar boundary mask.",
            )
            boundary_pixel_count += int(np.count_nonzero(inside))
            selected = values[valid]
            if selected.size:
                require(bool(np.all(selected >= 0)), "Population raster has negative valid population values.")
                valid_count += int(selected.size)
                minimum = min(minimum, float(selected.min()))
                maximum = max(maximum, float(selected.max()))
        require(valid_count > 0, "Population raster contains no valid pixels.")
        require(valid_count == metadata.get("valid_pixel_count"), "Population valid pixel count differs from metadata.")
        require(math.isclose(minimum, metadata.get("population_min", math.nan), rel_tol=1e-7), "Population minimum differs from metadata.")
        require(math.isclose(maximum, metadata.get("population_max", math.nan), rel_tol=1e-7), "Population maximum differs from metadata.")
        require(
            math.isclose(
                resolution[0] * 3600,
                metadata["resolution"].get("arc_seconds", [math.nan])[0],
                rel_tol=1e-7,
            ),
            "Population arc-second resolution differs from metadata.",
        )
    return {
        "source": metadata["source"],
        "year": metadata["year"],
        "resolution": metadata["resolution"],
        "crs": "EPSG:4326",
        "units": metadata["units"],
        "nodata": metadata["nodata"],
        "dtype": metadata["dtype"],
        "bounds": [bounds.left, bounds.bottom, bounds.right, bounds.top],
        "width": raster.width,
        "height": raster.height,
        "valid_pixel_count": valid_count,
        "population_min": minimum,
        "population_max": maximum,
        "bihar_boundary_pixel_count": boundary_pixel_count,
        "metadata": metadata,
    }


def main() -> int:
    try:
        boundary = load_boundary()
        buildings = validate_buildings(boundary)
        population = validate_population(boundary)
        summary = read_json(SUMMARY_PATH)
        require(summary.get("status") in ("prepared_pending_independent_validation", "validated"), "Step 2C summary has an unexpected status.")
        require(summary.get("buildings", {}).get("feature_count") == buildings["feature_count"], "Summary building count differs from validated output.")
        require(summary.get("buildings", {}).get("crs") == buildings["crs"], "Summary building CRS differs from validated output.")
        require(
            summary.get("buildings", {}).get("source") == buildings["metadata"].get("source"),
            "Summary building source differs from validated metadata.",
        )
        require(
            math.isclose(
                summary.get("buildings", {}).get("area_km2", math.nan),
                buildings["area_km2"],
                rel_tol=1e-7,
            ),
            "Summary building area differs from the independently recalculated area.",
        )
        require(
            summary.get("population", {}).get("source") == population["source"],
            "Summary population source differs from validated metadata.",
        )
        require(summary.get("population", {}).get("year") == population["year"], "Summary population year differs from validated output.")
        require(summary.get("population", {}).get("crs") == population["crs"], "Summary population CRS differs from validated output.")
        require(summary.get("population", {}).get("units") == population["units"], "Summary population units differ from validated output.")
        require(summary.get("population", {}).get("resolution") == population["resolution"], "Summary population resolution differs from validated output.")
        require(summary.get("population", {}).get("valid_pixel_count") == population["valid_pixel_count"], "Summary valid population pixel count differs from validated output.")
        summary["status"] = "validated"
        summary["validation"] = {
            "valid": True,
            "buildings": {
                "feature_count": buildings["feature_count"],
                "valid_feature_count": buildings["valid_feature_count"],
                "invalid_feature_count": buildings["invalid_feature_count"],
                "repaired_feature_count": buildings["repaired_feature_count"],
                "crs": buildings["crs"],
                "intersects_bihar": buildings["intersects_bihar"],
                "contained_in_bihar": buildings["contained_in_bihar"],
            },
            "population": {
                "valid_pixel_count": population["valid_pixel_count"],
                "crs": population["crs"],
                "nodata": population["nodata"],
                "units": population["units"],
                "source_year": population["year"],
                "bihar_boundary_mask_verified": True,
            },
        }
        SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "validated", "buildings": {k: v for k, v in buildings.items() if k != "metadata"}, "population": {k: v for k, v in population.items() if k != "metadata"}}, indent=2))
        return 0
    except Exception as error:
        print(f"STEP 2C VALIDATION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
