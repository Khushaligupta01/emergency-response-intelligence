"""Prepare real OSM building and WorldPop population data for Bihar."""

import hashlib
import json
import math
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import pyproj

os.environ["PROJ_LIB"] = pyproj.datadir.get_data_dir()

import numpy as np
import pyarrow as pa
import pyogrio
import rasterio
import shapely
from pyogrio.raw import open_arrow
from pyproj import CRS, Geod, Transformer
from rasterio.features import geometry_mask
from rasterio.windows import Window, from_bounds
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[2]
BOUNDARY_PATH = ROOT / "data/external/boundaries/bihar_boundary.geojson"
OSM_ARCHIVE = (
    ROOT
    / "data/external/osm/geofabrik/eastern-zone-261015-free.gpkg.zip"
)
OSM_EXTRACTED_GEOPACKAGE = (
    ROOT / "data/external/osm/geofabrik/eastern-zone-261015.gpkg"
)
OSM_GEOPACKAGE = (
    ROOT / "data/external/osm/geofabrik/eastern-zone-261015-indexed.gpkg"
)
OSM_LAYER = "gis_osm_buildings_a_free"
OSM_URL = (
    "https://download.geofabrik.de/asia/india/"
    "eastern-zone-261015-free.gpkg.zip"
)
OSM_DATE = "2026-10-15"
POPULATION_SOURCE = (
    ROOT / "data/external/population/worldpop/ind_ppp_2020_constrained.tif"
)
POPULATION_URL = (
    "https://data.worldpop.org/GIS/Population/"
    "Global_2000_2020_Constrained/2020/BSGM/IND/"
    "ind_ppp_2020_constrained.tif"
)
WORLDPOP_PAGE = "https://hub.worldpop.org/geodata/summary?id=49804"
BUILDINGS_OUTPUT = ROOT / "data/processed/osm/bihar_buildings.geojson"
BUILDINGS_METADATA = (
    ROOT / "data/processed/osm/bihar_buildings_metadata.json"
)
POPULATION_OUTPUT = (
    ROOT / "data/processed/population/bihar_population.tif"
)
POPULATION_METADATA = (
    ROOT / "data/processed/population/bihar_population_metadata.json"
)
SUMMARY_OUTPUT = ROOT / "data/processed/step2c_data_summary.json"
ANALYSIS_CRS = "EPSG:32645"
STORAGE_CRS = "EPSG:4326"
BOUNDARY_INSET_DEGREES = 1e-10


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_source(url: str, output: Path) -> None:
    if output.is_file() and output.stat().st_size:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".part")
    request = Request(url, headers={"User-Agent": "EmergencyResponseAI/1.0"})
    with urlopen(request, timeout=120) as response, temporary.open("wb") as target:
        expected_size = int(response.headers.get("Content-Length", "0"))
        downloaded = 0
        while True:
            block = response.read(8 * 1024 * 1024)
            if not block:
                break
            target.write(block)
            downloaded += len(block)
        require(
            not expected_size or downloaded == expected_size,
            f"Incomplete download for {url}: {downloaded}/{expected_size} bytes.",
        )
    temporary.replace(output)


def ensure_osm_geopackage() -> None:
    if OSM_EXTRACTED_GEOPACKAGE.is_file() and OSM_EXTRACTED_GEOPACKAGE.stat().st_size:
        return
    require(OSM_ARCHIVE.is_file(), f"OSM source archive is missing: {OSM_ARCHIVE}")
    OSM_EXTRACTED_GEOPACKAGE.parent.mkdir(parents=True, exist_ok=True)
    temporary = OSM_EXTRACTED_GEOPACKAGE.with_name(OSM_EXTRACTED_GEOPACKAGE.name + ".part")
    with zipfile.ZipFile(OSM_ARCHIVE) as archive:
        source_info = archive.getinfo("eastern-zone.gpkg")
        with archive.open(source_info) as source, temporary.open("wb") as target:
            written = 0
            while True:
                block = source.read(16 * 1024 * 1024)
                if not block:
                    break
                target.write(block)
                written += len(block)
            require(
                written == source_info.file_size,
                f"Extracted GeoPackage size mismatch: {written}/{source_info.file_size}.",
            )
    temporary.replace(OSM_EXTRACTED_GEOPACKAGE)


def ensure_osm_spatial_index() -> None:
    if OSM_GEOPACKAGE.is_file() and OSM_GEOPACKAGE.stat().st_size:
        with sqlite3.connect(f"file:{OSM_GEOPACKAGE}?mode=ro", uri=True) as connection:
            indexed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (f"rtree_{OSM_LAYER}_geom",),
            ).fetchone()
        require(indexed is not None, "Indexed OSM GeoPackage exists without its spatial index.")
        return

    require(OSM_EXTRACTED_GEOPACKAGE.is_file(), "Extracted Geofabrik GeoPackage is missing.")
    temporary = OSM_GEOPACKAGE.with_name(OSM_GEOPACKAGE.name + ".part")
    OSM_GEOPACKAGE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OSM_EXTRACTED_GEOPACKAGE, temporary)
    connection = sqlite3.connect(temporary)
    try:
        geometry_column = connection.execute(
            "SELECT column_name FROM gpkg_geometry_columns WHERE table_name=?",
            (OSM_LAYER,),
        ).fetchone()
        require(geometry_column is not None, f"OSM layer {OSM_LAYER} has no GeoPackage geometry column.")
        geometry_column = geometry_column[0]
        index_name = f"rtree_{OSM_LAYER}_{geometry_column}"
        extensions_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='gpkg_extensions'"
        ).fetchone()
        if extensions_table is None:
            connection.execute(
                "CREATE TABLE gpkg_extensions ("
                "table_name TEXT, column_name TEXT, extension_name TEXT NOT NULL, "
                "definition TEXT NOT NULL, scope TEXT NOT NULL, "
                "CONSTRAINT ge_tce UNIQUE (table_name,column_name,extension_name))"
            )
        connection.execute(
            f"CREATE VIRTUAL TABLE {index_name} USING rtree(id,minx,maxx,miny,maxy)"
        )
        select_sql = (
            f"SELECT fid,{geometry_column} FROM {OSM_LAYER} "
            f"WHERE {geometry_column} IS NOT NULL"
        )
        cursor = connection.execute(select_sql)
        processed = 0
        while batch := cursor.fetchmany(50000):
            index_rows = []
            for feature_id, geometry_blob in batch:
                flags = geometry_blob[3]
                envelope_type = (flags >> 1) & 7
                require(envelope_type in (1, 2, 3, 4), f"OSM feature {feature_id} has no GeoPackage XY envelope.")
                byte_order = "<" if flags & 1 else ">"
                minx, maxx, miny, maxy = struct.unpack_from(
                    byte_order + "dddd", geometry_blob, 8
                )
                require(
                    all(math.isfinite(value) for value in (minx, maxx, miny, maxy))
                    and minx <= maxx
                    and miny <= maxy,
                    f"OSM feature {feature_id} has an invalid GeoPackage envelope.",
                )
                index_rows.append((feature_id, minx, maxx, miny, maxy))
            connection.executemany(
                f"INSERT INTO {index_name} VALUES (?,?,?,?,?)", index_rows
            )
            processed += len(index_rows)
            connection.commit()
            print(f"Building Geofabrik spatial index: {processed:,} features")
        connection.execute(
            "INSERT INTO gpkg_extensions "
            "(table_name,column_name,extension_name,definition,scope) "
            "VALUES (?,?,?,?,?)",
            (
                OSM_LAYER,
                geometry_column,
                "gpkg_rtree_index",
                "http://www.geopackage.org/spec/#extension_rtree",
                "write-only",
            ),
        )
        connection.commit()
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        temporary.replace(OSM_GEOPACKAGE)


def load_boundary():
    require(BOUNDARY_PATH.is_file(), f"Bihar boundary is missing: {BOUNDARY_PATH}")
    with BOUNDARY_PATH.open(encoding="utf-8") as source:
        document = json.load(source)
    require(document.get("type") == "FeatureCollection", "Bihar boundary is not a FeatureCollection.")
    require(len(document.get("features", [])) == 1, "Expected one Bihar boundary feature.")
    boundary = shape(document["features"][0]["geometry"])
    require(not boundary.is_empty and boundary.is_valid, "Bihar boundary is invalid or empty.")
    boundary_crs_name = document.get("crs", {}).get("properties", {}).get("name")
    require(
        boundary_crs_name in (None, "urn:ogc:def:crs:OGC:1.3:CRS84"),
        f"Unexpected Bihar boundary CRS: {boundary_crs_name}",
    )
    return boundary, document


def polygonal_parts(geometry) -> tuple[list[Polygon], list[str]]:
    if isinstance(geometry, Polygon):
        return [geometry], []
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms), []
    if isinstance(geometry, GeometryCollection):
        polygons = []
        non_polygon_types = []
        for part in geometry.geoms:
            child_polygons, child_non_polygon_types = polygonal_parts(part)
            polygons.extend(child_polygons)
            non_polygon_types.extend(child_non_polygon_types)
        return polygons, non_polygon_types
    return [], [geometry.geom_type]


def as_valid_polygonal(geometry):
    if not shapely.is_valid(geometry):
        geometry = shapely.make_valid(geometry)
    polygons, non_polygon_types = polygonal_parts(geometry)
    require(polygons, f"Building has no polygonal part after repair: {non_polygon_types}")
    result = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    if not result.is_valid:
        result = shapely.make_valid(result)
        polygons, extra_non_polygon_types = polygonal_parts(result)
        non_polygon_types.extend(extra_non_polygon_types)
        require(polygons, "Building repair returned no polygonal parts.")
        result = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
    require(result.is_valid, "Building geometry remains invalid after make_valid().")
    return result, non_polygon_types


def _prepare_buildings_python(boundary, projected_boundary_area: float) -> dict:
    ensure_osm_geopackage()
    ensure_osm_spatial_index()
    require(OSM_ARCHIVE.is_file(), "Geofabrik raw OSM archive is missing.")
    require(OSM_GEOPACKAGE.is_file(), "Extracted Geofabrik source GeoPackage is missing.")
    info = __import__("pyogrio").read_info(OSM_GEOPACKAGE, layer=OSM_LAYER)
    source_count = int(info["features"])
    require(source_count > 0, "Geofabrik OSM building layer is empty.")
    source_crs = info.get("crs")
    require(source_crs and "WGS 84" in source_crs, "OSM building layer CRS is not WGS84.")
    require(info.get("geometry_type") in ("MultiPolygon", "Polygon"), "OSM building layer is not polygonal.")

    boundary_mask = boundary.buffer(-BOUNDARY_INSET_DEGREES)
    require(boundary_mask.is_valid and not boundary_mask.is_empty, "Bihar inset mask is invalid or empty.")
    bbox = boundary.bounds
    transformer = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
    projected_area_m2 = 0.0
    bbox_candidate_count = 0
    invalid_source_count = 0
    repaired_count = 0
    source_invalid_osm_ids = set()
    repaired_output_ids = set()
    repaired_type_changes = Counter()
    excluded_no_polygon_after_repair = []
    excluded_non_polygonal_only = Counter()
    clipped_away_count = 0
    output_count = 0
    valid_output_count = 0
    invalid_output_count = 0
    repaired_output_count = 0
    minx = miny = math.inf
    maxx = maxy = -math.inf
    output_path = BUILDINGS_OUTPUT.with_name(BUILDINGS_OUTPUT.name + ".part")
    BUILDINGS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as output:
        output.write('{"type":"FeatureCollection","name":"Bihar OSM building footprints",')
        output.write('"crs":{"type":"name","properties":{"name":"EPSG:4326"}},')
        output.write('"coordinate_reference_system":"EPSG:4326","features":[\n')
        first = True
        with open_arrow(
            OSM_GEOPACKAGE,
            layer=OSM_LAYER,
            bbox=bbox,
            columns=["osm_id", "code", "fclass", "name", "type"],
            batch_size=8192,
        ) as arrow_context:
            source_metadata, arrow_stream = arrow_context
            reader = pa.RecordBatchReader.from_stream(arrow_stream)
            require(
                source_metadata.get("geometry_type") in ("MultiPolygon", "Polygon"),
                "Arrow OSM stream reports a non-polygonal source layer.",
            )
            for batch in reader:
                columns = {
                    name: batch.column(name).to_pylist()
                    for name in batch.schema.names
                    if name != "geom"
                }
                geometries = shapely.from_wkb(batch.column("geom").to_pylist())
                bbox_candidate_count += len(geometries)
                for index, geometry in enumerate(geometries):
                    source_valid = bool(shapely.is_valid(geometry))
                    source_type = geometry.geom_type
                    geometry_repaired = not source_valid
                    repair_counted = False
                    if not source_valid:
                        invalid_source_count += 1
                        source_invalid_osm_ids.add(str(columns["osm_id"][index]))
                        geometry = shapely.make_valid(geometry)
                        repaired_type_changes[
                            f"{source_type}->{geometry.geom_type}"
                        ] += 1
                    polygons, non_polygon_types = polygonal_parts(geometry)
                    if not polygons:
                        excluded_no_polygon_after_repair.append(
                            {
                                "osm_id": columns["osm_id"][index],
                                "reason": "No polygonal part after source geometry validation/repair.",
                                "source_geometry_valid": source_valid,
                                "source_geometry_type": source_type,
                                "repair_result_type": geometry.geom_type,
                                "non_polygon_types": non_polygon_types,
                            }
                        )
                        continue
                    polygonal = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
                    if not polygonal.is_valid:
                        before_type = polygonal.geom_type
                        polygonal = shapely.make_valid(polygonal)
                        geometry_repaired = True
                        repaired_type_changes[
                            f"{before_type}->{polygonal.geom_type}"
                        ] += 1
                        polygons, extra_non_polygon_types = polygonal_parts(polygonal)
                        non_polygon_types.extend(extra_non_polygon_types)
                        require(polygons, "OSM geometry repair produced no polygonal component.")
                        polygonal = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
                    require(polygonal.is_valid, "OSM polygon is still invalid after repair.")
                    if geometry_repaired:
                        repaired_count += 1
                        repair_counted = True
                    if non_polygon_types:
                        excluded_non_polygonal_only.update(non_polygon_types)
                    if not polygonal.intersects(boundary_mask):
                        clipped_away_count += 1
                        continue
                    clipped = polygonal.intersection(boundary_mask)
                    clipped_polygons, clipped_non_polygon_types = polygonal_parts(clipped)
                    if not clipped_polygons:
                        clipped_away_count += 1
                        excluded_non_polygonal_only.update(clipped_non_polygon_types)
                        continue
                    clipped_geometry = (
                        clipped_polygons[0]
                        if len(clipped_polygons) == 1
                        else MultiPolygon(clipped_polygons)
                    )
                    if not clipped_geometry.is_valid:
                        before_type = clipped_geometry.geom_type
                        clipped_geometry = shapely.make_valid(clipped_geometry)
                        geometry_repaired = True
                        if not repair_counted:
                            repaired_count += 1
                            repair_counted = True
                        repaired_type_changes[
                            f"{before_type}->{clipped_geometry.geom_type}"
                        ] += 1
                        clipped_polygons, extra_types = polygonal_parts(clipped_geometry)
                        clipped_non_polygon_types.extend(extra_types)
                        require(clipped_polygons, "Clipped building repair produced no polygons.")
                        clipped_geometry = (
                            clipped_polygons[0]
                            if len(clipped_polygons) == 1
                            else MultiPolygon(clipped_polygons)
                        )
                    require(clipped_geometry.is_valid and not clipped_geometry.is_empty, "Invalid clipped building.")
                    require(boundary.covers(clipped_geometry), "A clipped building lies outside Bihar.")
                    projected = shapely.transform(
                        clipped_geometry, transformer.transform, interleaved=False
                    )
                    footprint_area = float(projected.area)
                    require(footprint_area > 0, "Clipped building has non-positive projected area.")
                    projected_area_m2 += footprint_area
                    bx1, by1, bx2, by2 = clipped_geometry.bounds
                    minx, miny = min(minx, bx1), min(miny, by1)
                    maxx, maxy = max(maxx, bx2), max(maxy, by2)
                    output_feature = {
                        "type": "Feature",
                        "properties": {
                            "osm_id": columns["osm_id"][index],
                            "code": columns["code"][index],
                            "fclass": columns["fclass"][index],
                            "name": columns["name"][index],
                            "type": columns["type"][index],
                            "source_geometry_type": source_type,
                            "source_geometry_valid": source_valid,
                            "source_geometry_repaired": not source_valid,
                            "geometry_repaired_during_processing": geometry_repaired,
                            "source_non_polygonal_parts_excluded": non_polygon_types,
                            "clip_non_polygonal_parts_excluded": clipped_non_polygon_types,
                            "source": "OpenStreetMap via Geofabrik",
                        },
                        "geometry": mapping(clipped_geometry),
                    }
                    if not first:
                        output.write(",\n")
                    json.dump(output_feature, output, ensure_ascii=False, separators=(",", ":"))
                    first = False
                    output_count += 1
                    valid_output_count += int(clipped_geometry.is_valid)
                    invalid_output_count += int(not clipped_geometry.is_valid)
                    repaired_output_count += int(geometry_repaired)
                    if geometry_repaired:
                        repaired_output_ids.add(str(columns["osm_id"][index]))
                print(
                    f"OSM candidates {bbox_candidate_count:,}; "
                    f"Bihar buildings written {output_count:,}",
                    flush=True,
                )
        output.write("\n]}\n")
    require(output_count > 0, "No OSM building footprints intersect Bihar.")
    require(invalid_output_count == 0, "Invalid building geometry remained in output.")
    output_path.replace(BUILDINGS_OUTPUT)

    archive_size = OSM_ARCHIVE.stat().st_size
    gpkg_size = OSM_GEOPACKAGE.stat().st_size
    metadata = {
        "source": "OpenStreetMap (OSM) contributors, downloaded through Geofabrik",
        "source_url": OSM_URL,
        "source_region": "Geofabrik Eastern Zone (regional extract includes Bihar)",
        "source_snapshot_date": OSM_DATE,
        "source_archive_modified_at_utc": datetime.fromtimestamp(
            OSM_ARCHIVE.stat().st_mtime, timezone.utc
        ).isoformat(),
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(),
        "license": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
        "geofabrik_download_page": "https://download.geofabrik.de/asia/india/eastern-zone.html",
        "geofabrik_extract_technical_notes": "https://download.geofabrik.de/technical.html",
        "source_archive": str(OSM_ARCHIVE.relative_to(ROOT)),
        "source_archive_bytes": archive_size,
        "source_archive_sha256": sha256(OSM_ARCHIVE),
        "source_geopackage": str(OSM_GEOPACKAGE.relative_to(ROOT)),
        "source_geopackage_bytes": gpkg_size,
        "source_geopackage_sha256": sha256(OSM_GEOPACKAGE),
        "source_unindexed_geopackage": str(OSM_EXTRACTED_GEOPACKAGE.relative_to(ROOT)),
        "spatial_index_processing": "Added a GeoPackage R-tree index to a derived copy of the extracted source to enable efficient bounding-box filtering; source feature records remain unchanged.",
        "source_layer": OSM_LAYER,
        "source_layer_feature_count": source_count,
        "source_layer_geometry_type": info["geometry_type"],
        "source_crs": STORAGE_CRS,
        "boundary": {
            "name": "Bihar",
            "source": "geoBoundaries gbOpen India ADM1",
            "file": str(BOUNDARY_PATH.relative_to(ROOT)),
            "crs": STORAGE_CRS,
            "bounds": list(boundary.bounds),
            "area_km2_epsg32645": projected_boundary_area / 1_000_000,
        },
        "processing_steps": [
            "Read the Geofabrik eastern-zone GeoPackage OSM building polygon layer.",
            "Add a GeoPackage R-tree index to a derived copy of the source GeoPackage; preserve the downloaded archive and unindexed extraction.",
            "Spatially filter real source features to the Bihar boundary bounding box.",
            "Check source validity; run Shapely make_valid only for invalid candidate geometries and record repaired IDs/counts and type changes.",
            "Retain polygonal parts and intersect each source building with a 1e-10 degree inward Bihar boundary mask.",
            "Preserve each OSM feature separately with source osm_id and Geofabrik source attributes; do not synthesize, grid, or dissolve buildings.",
            f"Write output coordinates in {STORAGE_CRS}; calculate footprint areas in {ANALYSIS_CRS}.",
        ],
        "crs": STORAGE_CRS,
        "output_file": str(BUILDINGS_OUTPUT.relative_to(ROOT)),
        "feature_count": output_count,
        "valid_building_features": valid_output_count,
        "invalid_building_features": invalid_output_count,
        "repaired_building_features": repaired_output_count,
        "source_bbox_candidate_count": bbox_candidate_count,
        "source_invalid_geometry_count_in_bihar_bbox": invalid_source_count,
        "source_repaired_geometry_count_in_bihar_bbox": repaired_count,
        "source_invalid_osm_ids_in_bihar_bbox": sorted(source_invalid_osm_ids),
        "source_invalid_osm_ids_with_clipped_output": sorted(repaired_output_ids),
        "clip_repaired_osm_ids": [],
        "source_repair_result_type_changes": dict(repaired_type_changes),
        "features_excluded_after_repair": excluded_no_polygon_after_repair,
        "non_polygonal_parts_excluded_counts": dict(excluded_non_polygonal_only),
        "features_not_intersecting_bihar": clipped_away_count,
        "area_calculation_crs": ANALYSIS_CRS,
        "total_building_footprint_area_km2": projected_area_m2 / 1_000_000,
        "area_sum_note": "Sum of individual clipped OSM footprint areas; overlapping mapped footprints, if present, are counted separately.",
        "bounding_box_epsg4326": [minx, miny, maxx, maxy],
        "coverage_relative_to_bihar": {
            "all_output_features_intersect_bihar": True,
            "all_output_features_contained_by_bihar": True,
            "mapped_footprint_area_percent_of_bihar": (
                projected_area_m2 / projected_boundary_area * 100
            ),
            "interpretation": "Mapped footprint area as a proportion of projected Bihar boundary area, not a completeness estimate; OSM is not guaranteed to map every building.",
        },
        "synthetic_generation_used": False,
        "limitations": [
            "OSM building coverage is incomplete and varies geographically; absence of a footprint does not mean no building exists.",
            "Geofabrik free GeoPackage extracts retain selected OSM attributes and are not a full history-preserving OSM dump.",
            "The requested Bihar clip uses the authoritative project boundary; a 1e-10 degree inward tolerance removes only numerical edge slivers.",
            "Footprint area sums can double-count overlapping mapped polygons.",
        ],
    }
    BUILDINGS_METADATA.parent.mkdir(parents=True, exist_ok=True)
    BUILDINGS_METADATA.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def prepare_buildings_ogr(boundary, projected_boundary_area: float) -> dict:
    ensure_osm_geopackage()
    ensure_osm_spatial_index()
    require(OSM_ARCHIVE.is_file(), f"Geofabrik raw OSM archive is missing: {OSM_ARCHIVE}")
    require(OSM_GEOPACKAGE.is_file(), f"Indexed Geofabrik source GeoPackage is missing: {OSM_GEOPACKAGE}")
    ogr2ogr = shutil.which("ogr2ogr")
    require(ogr2ogr is not None, "ogr2ogr is required for efficient clipping of this regional OSM extract.")
    info = pyogrio.read_info(OSM_GEOPACKAGE, layer=OSM_LAYER)
    source_count = int(info["features"])
    require(source_count > 0, "Geofabrik OSM building layer is empty.")
    require(info.get("crs") and "WGS 84" in info["crs"], "OSM building layer CRS is not WGS84.")
    require(info.get("geometry_type") in ("MultiPolygon", "Polygon"), "OSM building layer is not polygonal.")

    bbox = boundary.bounds
    invalid_source_ids = set()
    source_invalid_geometry_types = Counter()
    bbox_candidate_count = 0
    with open_arrow(
        OSM_GEOPACKAGE,
        layer=OSM_LAYER,
        bbox=bbox,
        columns=["osm_id"],
        batch_size=65536,
    ) as arrow_context:
        _, arrow_stream = arrow_context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        for batch in reader:
            geometries = shapely.from_wkb(batch.column("geom").to_pylist())
            identifiers = batch.column("osm_id").to_pylist()
            bbox_candidate_count += len(geometries)
            valid = shapely.is_valid(geometries)
            for index in np.flatnonzero(~valid):
                invalid_source_ids.add(str(identifiers[index]))
                source_invalid_geometry_types[geometries[index].geom_type] += 1
    if invalid_source_ids:
        return _prepare_buildings_python(boundary, projected_boundary_area)

    if invalid_source_ids:
        return _prepare_buildings_python(boundary, projected_boundary_area)

    output_path = BUILDINGS_OUTPUT.with_name(BUILDINGS_OUTPUT.name + ".part")
    output_path.unlink(missing_ok=True)
    BUILDINGS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ogr2ogr,
        "-f",
        "GeoJSON",
        str(output_path),
        str(OSM_GEOPACKAGE),
        OSM_LAYER,
        "-spat",
        str(bbox[0]),
        str(bbox[1]),
        str(bbox[2]),
        str(bbox[3]),
        "-clipsrc",
        str(BOUNDARY_PATH),
        "-nlt",
        "PROMOTE_TO_MULTI",
        "-lco",
        "RFC7946=YES",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    require(
        result.returncode == 0,
        f"ogr2ogr failed to clip OSM buildings (exit {result.returncode}): {result.stderr.strip()}",
    )
    require(output_path.is_file() and output_path.stat().st_size > 0, "ogr2ogr did not create the clipped OSM GeoJSON.")

    output_info = pyogrio.read_info(output_path)
    require(output_info.get("driver") == "GeoJSON", "ogr2ogr output is not GeoJSON.")
    require(CRS.from_user_input(output_info.get("crs")).to_epsg() == 4326, "Clipped building output CRS is not EPSG:4326.")
    transformer = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
    projected_area_m2 = 0.0
    output_count = valid_output_count = invalid_output_count = repaired_output_count = 0
    minx = miny = math.inf
    maxx = maxy = -math.inf
    output_invalid_source_ids = set()
    with open_arrow(output_path, batch_size=65536) as arrow_context:
        output_metadata, arrow_stream = arrow_context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        require("osm_id" in reader.schema.names, "Clipped building GeoJSON does not preserve OSM IDs.")
        geometry_name = output_metadata.get("geometry_name")
        if geometry_name not in reader.schema.names:
            geometry_columns = [
                field.name
                for field in reader.schema
                if pa.types.is_binary(field.type) or pa.types.is_large_binary(field.type)
            ]
            require(len(geometry_columns) == 1, f"Cannot identify output GeoJSON geometry column: {reader.schema}")
            geometry_name = geometry_columns[0]
        for batch in reader:
            geometries = shapely.from_wkb(batch.column(geometry_name).to_pylist())
            identifiers = batch.column("osm_id").to_pylist()
            output_count += len(geometries)
            valid = shapely.is_valid(geometries)
            empty = shapely.is_empty(geometries)
            polygonal = np.isin(shapely.get_type_id(geometries), [3, 6])
            valid_output_count += int(np.count_nonzero(valid & ~empty & polygonal))
            invalid_output_count += int(np.count_nonzero(~valid | empty | ~polygonal))
            require(bool(np.all(valid & ~empty & polygonal)), "ogr2ogr output contains invalid, empty, or non-polygonal geometry.")
            require(bool(np.all(shapely.covers(boundary, geometries))), "ogr2ogr output contains geometry outside Bihar.")
            projected = shapely.transform(geometries, transformer.transform, interleaved=False)
            projected_area_m2 += float(np.sum(shapely.area(projected)))
            bounds = shapely.bounds(geometries)
            if len(bounds):
                minx = min(minx, float(np.min(bounds[:, 0])))
                miny = min(miny, float(np.min(bounds[:, 1])))
                maxx = max(maxx, float(np.max(bounds[:, 2])))
                maxy = max(maxy, float(np.max(bounds[:, 3])))
            for identifier in identifiers:
                normalized_id = str(identifier)
                if normalized_id in invalid_source_ids:
                    output_invalid_source_ids.add(normalized_id)

    require(output_count > 0, "No OSM building footprints intersect Bihar.")
    require(projected_area_m2 > 0, "Clipped building footprint area is not positive.")
    repaired_output_count = len(output_invalid_source_ids)
    output_path.replace(BUILDINGS_OUTPUT)
    archive_size = OSM_ARCHIVE.stat().st_size
    gpkg_size = OSM_GEOPACKAGE.stat().st_size
    metadata = {
        "source": "OpenStreetMap (OSM) contributors, downloaded through Geofabrik",
        "source_url": OSM_URL,
        "source_region": "Geofabrik Eastern Zone (regional extract includes Bihar)",
        "source_snapshot_date": OSM_DATE,
        "source_archive_modified_at_utc": datetime.fromtimestamp(
            OSM_ARCHIVE.stat().st_mtime, timezone.utc
        ).isoformat(),
        "processed_at_utc": datetime.now(timezone.utc).isoformat(),
        "license": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
        "geofabrik_download_page": "https://download.geofabrik.de/asia/india/eastern-zone.html",
        "geofabrik_extract_technical_notes": "https://download.geofabrik.de/technical.html",
        "source_archive": str(OSM_ARCHIVE.relative_to(ROOT)),
        "source_archive_bytes": archive_size,
        "source_archive_sha256": sha256(OSM_ARCHIVE),
        "source_geopackage": str(OSM_GEOPACKAGE.relative_to(ROOT)),
        "source_geopackage_bytes": gpkg_size,
        "source_geopackage_sha256": sha256(OSM_GEOPACKAGE),
        "source_unindexed_geopackage": str(OSM_EXTRACTED_GEOPACKAGE.relative_to(ROOT)),
        "spatial_index_processing": "Added a GeoPackage R-tree index to a derived copy of the extracted source to enable efficient bounding-box filtering; downloaded archive and extracted feature records remain unchanged.",
        "source_layer": OSM_LAYER,
        "source_layer_feature_count": source_count,
        "source_layer_geometry_type": info["geometry_type"],
        "source_crs": STORAGE_CRS,
        "boundary": {
            "name": "Bihar",
            "source": "geoBoundaries gbOpen India ADM1",
            "file": str(BOUNDARY_PATH.relative_to(ROOT)),
            "crs": STORAGE_CRS,
            "bounds": list(boundary.bounds),
            "area_km2_epsg32645": projected_boundary_area / 1_000_000,
        },
        "processing_steps": [
            "Read the Geofabrik eastern-zone GeoPackage OSM building polygon layer.",
            "Preserve the downloaded source archive and unindexed extracted GeoPackage; add a spatial index to a derived working copy.",
            "Check validity of source features in the Bihar bounding box and record invalid OSM IDs and source geometry types.",
            "Use GDAL ogr2ogr to clip valid source geometries against the authoritative Bihar boundary; invalid source geometries use the Shapely make_valid processing path.",
            "Promote output geometries to MultiPolygon; one-part Polygon geometries are represented as MultiPolygon.",
            f"Write coordinates in {STORAGE_CRS}; calculate footprint areas in {ANALYSIS_CRS}.",
            "Preserve each source OSM feature and osm_id; do not synthesize, grid, or dissolve buildings.",
        ],
        "crs": STORAGE_CRS,
        "output_file": str(BUILDINGS_OUTPUT.relative_to(ROOT)),
        "feature_count": output_count,
        "valid_building_features": valid_output_count,
        "invalid_building_features": invalid_output_count,
        "repaired_building_features": repaired_output_count,
        "source_bbox_candidate_count": bbox_candidate_count,
        "source_invalid_geometry_count_in_bihar_bbox": len(invalid_source_ids),
        "source_repaired_geometry_count_in_bihar_bbox": len(invalid_source_ids),
        "source_invalid_geometry_types": dict(source_invalid_geometry_types),
        "source_invalid_osm_ids_in_bihar_bbox": sorted(invalid_source_ids),
        "source_invalid_osm_ids_with_clipped_output": sorted(output_invalid_source_ids),
        "source_invalid_osm_ids_without_clipped_output": sorted(invalid_source_ids - output_invalid_source_ids),
        "source_repair_method": "The accelerated GDAL clipping path is used only when all source geometries in the Bihar bounding box are valid; invalid features are instead repaired with Shapely make_valid and fully recorded.",
        "features_not_intersecting_bihar": bbox_candidate_count - output_count,
        "area_calculation_crs": ANALYSIS_CRS,
        "total_building_footprint_area_km2": projected_area_m2 / 1_000_000,
        "area_sum_note": "Sum of individual clipped OSM footprint areas; overlapping mapped polygons, if present, are counted separately.",
        "bounding_box_epsg4326": [minx, miny, maxx, maxy],
        "coverage_relative_to_bihar": {
            "all_output_features_intersect_bihar": True,
            "all_output_features_contained_by_bihar": True,
            "mapped_footprint_area_percent_of_bihar": projected_area_m2 / projected_boundary_area * 100,
            "interpretation": "Mapped footprint area as a proportion of projected Bihar boundary area, not a completeness estimate; OSM is not guaranteed to map every building.",
        },
        "synthetic_generation_used": False,
        "limitations": [
            "OSM building coverage is incomplete and varies geographically; absence of a footprint does not mean no building exists.",
            "Geofabrik free GeoPackage extracts retain selected OSM attributes and are not a full history-preserving OSM dump.",
            "Footprint area sums can double-count overlapping mapped polygons.",
        ],
    }
    BUILDINGS_METADATA.parent.mkdir(parents=True, exist_ok=True)
    BUILDINGS_METADATA.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def prepare_buildings_vectorized(boundary, projected_boundary_area: float) -> dict:
    ensure_osm_geopackage()
    ensure_osm_spatial_index()
    info = pyogrio.read_info(OSM_GEOPACKAGE, layer=OSM_LAYER)
    source_count = int(info["features"])
    bbox = boundary.bounds
    clip_boundary = boundary.buffer(-BOUNDARY_INSET_DEGREES)
    require(
        clip_boundary.is_valid and not clip_boundary.is_empty,
        "Numerically inset Bihar clip boundary is invalid or empty.",
    )
    invalid_source_ids = set()
    source_invalid_geometry_types = Counter()
    bbox_candidate_count = 0
    with open_arrow(
        OSM_GEOPACKAGE,
        layer=OSM_LAYER,
        bbox=bbox,
        columns=["osm_id"],
        batch_size=65536,
    ) as arrow_context:
        _, arrow_stream = arrow_context
        reader = pa.RecordBatchReader.from_stream(arrow_stream)
        for batch in reader:
            geometries = shapely.from_wkb(batch.column("geom").to_pylist())
            identifiers = batch.column("osm_id").to_pylist()
            bbox_candidate_count += len(geometries)
            for index in np.flatnonzero(~shapely.is_valid(geometries)):
                invalid_source_ids.add(str(identifiers[index]))
                source_invalid_geometry_types[geometries[index].geom_type] += 1
    if invalid_source_ids:
        return _prepare_buildings_python(boundary, projected_boundary_area)

    output_path = BUILDINGS_OUTPUT.with_name(BUILDINGS_OUTPUT.name + ".part")
    output_path.unlink(missing_ok=True)
    BUILDINGS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    transformer = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
    projected_area_m2 = 0.0
    output_count = repaired_count = 0
    repaired_osm_ids = set()
    non_polygonal_parts_excluded = Counter()
    clipped_without_polygon_count = 0
    minx = miny = math.inf
    maxx = maxy = -math.inf
    with output_path.open("w", encoding="utf-8", newline="") as output:
        output.write('{"type":"FeatureCollection","name":"Bihar OSM building footprints",')
        output.write('"crs":{"type":"name","properties":{"name":"EPSG:4326"}},')
        output.write('"coordinate_reference_system":"EPSG:4326","features":[\n')
        first = True
        with open_arrow(
            OSM_GEOPACKAGE,
            layer=OSM_LAYER,
            bbox=bbox,
            columns=["osm_id", "code", "fclass", "name", "type"],
            batch_size=8192,
        ) as arrow_context:
            _, arrow_stream = arrow_context
            reader = pa.RecordBatchReader.from_stream(arrow_stream)
            for batch in reader:
                columns = {
                    name: batch.column(name).to_pylist()
                    for name in batch.schema.names
                    if name != "geom"
                }
                source_geometries = shapely.from_wkb(batch.column("geom").to_pylist())
                require(
                    bool(np.all(shapely.is_valid(source_geometries))),
                    "OSM source geometry validity changed between the validation and clipping passes.",
                )
                intersects = shapely.intersects(source_geometries, clip_boundary)
                source_indices = np.flatnonzero(intersects)
                if not len(source_indices):
                    continue
                clipped = shapely.intersection(source_geometries[source_indices], clip_boundary)
                clipped_types = shapely.get_type_id(clipped)
                clipped_valid = shapely.is_valid(clipped)
                per_feature_non_polygonal = [[] for _ in source_indices]
                per_feature_repaired = [False for _ in source_indices]
                keep = np.isin(clipped_types, [3, 6]) & ~shapely.is_empty(clipped)
                invalid_clip_indices = np.flatnonzero(~clipped_valid & ~shapely.is_empty(clipped))
                for local_index in invalid_clip_indices:
                    before_type = clipped[local_index].geom_type
                    repaired = shapely.make_valid(clipped[local_index])
                    polygons, non_polygon_types = polygonal_parts(repaired)
                    per_feature_non_polygonal[local_index].extend(non_polygon_types)
                    if polygons:
                        clipped[local_index] = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
                        clipped_types[local_index] = shapely.get_type_id(clipped[local_index])
                        clipped_valid[local_index] = shapely.is_valid(clipped[local_index])
                        keep[local_index] = True
                        repaired_count += 1
                        per_feature_repaired[local_index] = True
                    non_polygonal_parts_excluded.update(non_polygon_types)
                    require(
                        clipped_valid[local_index],
                        f"Clip repair left OSM feature {columns['osm_id'][source_indices[local_index]]} invalid.",
                    )
                collection_indices = np.flatnonzero(
                    (clipped_types == 7) & ~shapely.is_empty(clipped)
                )
                for local_index in collection_indices:
                    polygons, non_polygon_types = polygonal_parts(clipped[local_index])
                    per_feature_non_polygonal[local_index].extend(non_polygon_types)
                    non_polygonal_parts_excluded.update(non_polygon_types)
                    if polygons:
                        clipped[local_index] = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
                        keep[local_index] = True
                    else:
                        keep[local_index] = False
                clipped_without_polygon_count += int(np.count_nonzero(~keep))
                selected_indices = np.flatnonzero(keep)
                if not len(selected_indices):
                    continue
                selected = clipped[selected_indices]
                selected_types = shapely.get_type_id(selected)
                for local_index, geometry_type in enumerate(selected_types):
                    if geometry_type == 3:
                        selected[local_index] = MultiPolygon([selected[local_index]])
                require(
                    bool(np.all(shapely.is_valid(selected)))
                    and bool(np.all(~shapely.is_empty(selected)))
                    and bool(np.all(np.isin(shapely.get_type_id(selected), [3, 6]))),
                    "Clipped OSM output contains invalid, empty, or non-polygonal geometry.",
                )
                require(bool(np.all(shapely.covers(boundary, selected))), "Clipped OSM geometry is outside Bihar.")
                projected = shapely.transform(selected, transformer.transform, interleaved=False)
                areas = shapely.area(projected)
                require(bool(np.all(areas > 0)), "A clipped OSM footprint has non-positive projected area.")
                projected_area_m2 += float(np.sum(areas))
                output_bounds = shapely.bounds(selected)
                minx = min(minx, float(np.min(output_bounds[:, 0])))
                miny = min(miny, float(np.min(output_bounds[:, 1])))
                maxx = max(maxx, float(np.max(output_bounds[:, 2])))
                maxy = max(maxy, float(np.max(output_bounds[:, 3])))
                geometry_json = shapely.to_geojson(selected)
                for output_index, local_index in enumerate(selected_indices):
                    source_index = int(source_indices[local_index])
                    if per_feature_repaired[local_index]:
                        repaired_osm_ids.add(str(columns["osm_id"][source_index]))
                    feature_properties = {
                        "osm_id": columns["osm_id"][source_index],
                        "code": columns["code"][source_index],
                        "fclass": columns["fclass"][source_index],
                        "name": columns["name"][source_index],
                        "type": columns["type"][source_index],
                        "source_geometry_type": source_geometries[source_index].geom_type,
                        "source_geometry_valid": True,
                        "source_geometry_repaired": False,
                        "geometry_repaired_during_processing": per_feature_repaired[local_index],
                        "source_non_polygonal_parts_excluded": [],
                        "clip_non_polygonal_parts_excluded": per_feature_non_polygonal[local_index],
                        "source": "OpenStreetMap via Geofabrik",
                    }
                    if not first:
                        output.write(",\n")
                    output.write('{"type":"Feature","properties":')
                    json.dump(feature_properties, output, ensure_ascii=False, separators=(",", ":"))
                    output.write(',"geometry":')
                    output.write(str(geometry_json[output_index]))
                    output.write("}")
                    first = False
                    output_count += 1
                print(
                    f"OSM candidates processed {bbox_candidate_count:,}; Bihar buildings written {output_count:,}",
                    flush=True,
                )
        output.write("\n]}\n")
    require(output_count > 0, "No OSM building footprints intersect Bihar.")
    require(projected_area_m2 > 0, "Clipped OSM building footprint area is not positive.")
    output_path.replace(BUILDINGS_OUTPUT)

    archive_size = OSM_ARCHIVE.stat().st_size
    metadata = {
        "source": "OpenStreetMap (OSM) contributors, downloaded through Geofabrik",
        "source_url": OSM_URL,
        "source_region": "Geofabrik Eastern Zone (regional extract includes Bihar)",
        "source_snapshot_date": OSM_DATE,
        "processed_at_utc": datetime.now(timezone.utc).isoformat(),
        "license": "Open Database License (ODbL) 1.0",
        "attribution": "© OpenStreetMap contributors",
        "license_url": "https://www.openstreetmap.org/copyright",
        "geofabrik_download_page": "https://download.geofabrik.de/asia/india/eastern-zone.html",
        "geofabrik_extract_technical_notes": "https://download.geofabrik.de/technical.html",
        "source_archive": str(OSM_ARCHIVE.relative_to(ROOT)),
        "source_archive_bytes": archive_size,
        "source_archive_sha256": sha256(OSM_ARCHIVE),
        "source_geopackage": str(OSM_GEOPACKAGE.relative_to(ROOT)),
        "source_geopackage_bytes": OSM_GEOPACKAGE.stat().st_size,
        "source_geopackage_sha256": sha256(OSM_GEOPACKAGE),
        "source_unindexed_geopackage": str(OSM_EXTRACTED_GEOPACKAGE.relative_to(ROOT)),
        "spatial_index_processing": "Added a GeoPackage R-tree index to a derived copy of the extracted source; downloaded archive and extracted feature records remain unchanged.",
        "source_layer": OSM_LAYER,
        "source_layer_feature_count": source_count,
        "source_layer_geometry_type": info["geometry_type"],
        "source_crs": STORAGE_CRS,
        "boundary": {
            "name": "Bihar",
            "source": "geoBoundaries gbOpen India ADM1",
            "file": str(BOUNDARY_PATH.relative_to(ROOT)),
            "crs": STORAGE_CRS,
            "bounds": list(boundary.bounds),
            "area_km2_epsg32645": projected_boundary_area / 1_000_000,
        },
        "processing_steps": [
            "Read the Geofabrik Eastern Zone GeoPackage OSM building polygon layer.",
            "Use an R-tree index on a derived source copy for the Bihar bounding-box filter.",
            "Check source geometry validity and use Shapely vectorized intersection against a 1e-10 degree inward Bihar boundary mask to remove numerical edge slivers.",
            "Preserve each OSM feature separately and retain its source osm_id; do not synthesize, grid, or dissolve buildings.",
            f"Write GeoJSON coordinates in {STORAGE_CRS}; calculate footprint area in {ANALYSIS_CRS}.",
        ],
        "crs": STORAGE_CRS,
        "output_file": str(BUILDINGS_OUTPUT.relative_to(ROOT)),
        "feature_count": output_count,
        "valid_building_features": output_count,
        "invalid_building_features": 0,
        "repaired_building_features": repaired_count,
        "source_bbox_candidate_count": bbox_candidate_count,
        "source_invalid_geometry_count_in_bihar_bbox": 0,
        "source_repaired_geometry_count_in_bihar_bbox": 0,
        "source_invalid_geometry_types": {},
        "source_invalid_osm_ids_in_bihar_bbox": [],
        "clip_repaired_osm_ids": sorted(repaired_osm_ids),
        "source_repair_result_type_changes": {},
        "features_excluded_after_repair": [],
        "non_polygonal_parts_excluded_counts": dict(non_polygonal_parts_excluded),
        "features_without_polygon_after_clip": clipped_without_polygon_count,
        "features_not_intersecting_bihar": bbox_candidate_count - output_count,
        "area_calculation_crs": ANALYSIS_CRS,
        "total_building_footprint_area_km2": projected_area_m2 / 1_000_000,
        "area_sum_note": "Sum of individual clipped OSM footprint areas; overlapping mapped polygons, if present, are counted separately.",
        "bounding_box_epsg4326": [minx, miny, maxx, maxy],
        "coverage_relative_to_bihar": {
            "all_output_features_intersect_bihar": True,
            "all_output_features_contained_by_bihar": True,
            "mapped_footprint_area_percent_of_bihar": projected_area_m2 / projected_boundary_area * 100,
            "interpretation": "Mapped footprint area as a proportion of projected Bihar boundary area, not a completeness estimate; OSM is not guaranteed to map every building.",
        },
        "synthetic_generation_used": False,
        "limitations": [
            "OSM building coverage is incomplete and varies geographically; absence of a footprint does not mean no building exists.",
            "Geofabrik free GeoPackage extracts retain selected OSM attributes and are not a full history-preserving OSM dump.",
            "Footprint area sums can double-count overlapping mapped polygons.",
        ],
    }
    BUILDINGS_METADATA.parent.mkdir(parents=True, exist_ok=True)
    BUILDINGS_METADATA.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def prepare_buildings(boundary, projected_boundary_area: float) -> dict:
    return prepare_buildings_vectorized(boundary, projected_boundary_area)


def prepare_population(boundary) -> dict:
    require(POPULATION_SOURCE.is_file(), f"WorldPop raster is missing: {POPULATION_SOURCE}")
    POPULATION_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = POPULATION_OUTPUT.with_name(POPULATION_OUTPUT.name + ".part")
    with rasterio.open(POPULATION_SOURCE) as source:
        require(source.count == 1, "WorldPop source must have exactly one band.")
        require(source.crs is not None, "WorldPop source raster has no CRS.")
        require(source.nodata is not None, "WorldPop source raster has no NoData value.")
        require(source.dtypes[0] == "float32", f"Unexpected WorldPop source dtype: {source.dtypes[0]}")
        source_crs = CRS.from_user_input(source.crs)
        require(
            source_crs.equals(CRS.from_epsg(4326), ignore_axis_order=True),
            f"Unexpected WorldPop source CRS: {source.crs}",
        )
        require(
            source.bounds.left <= boundary.bounds[0]
            and source.bounds.bottom <= boundary.bounds[1]
            and source.bounds.right >= boundary.bounds[2]
            and source.bounds.top >= boundary.bounds[3],
            "WorldPop source bounds do not cover the Bihar boundary.",
        )
        src_window = from_bounds(*boundary.bounds, transform=source.transform)
        col_start = max(0, math.floor(src_window.col_off))
        row_start = max(0, math.floor(src_window.row_off))
        col_stop = min(source.width, math.ceil(src_window.col_off + src_window.width))
        row_stop = min(source.height, math.ceil(src_window.row_off + src_window.height))
        width = col_stop - col_start
        height = row_stop - row_start
        require(width > 0 and height > 0, "Bihar crop window has no raster pixels.")
        window = Window(col_start, row_start, width, height)
        out_transform = source.window_transform(window)
        profile = source.profile.copy()
        profile.update(
            driver="GTiff",
            width=width,
            height=height,
            transform=out_transform,
            count=1,
            tiled=True,
            blockxsize=512,
            blockysize=512,
            compress="deflate",
            predictor=3,
            BIGTIFF="IF_SAFER",
            crs=STORAGE_CRS,
        )
        nodata = source.nodata
        valid_count = 0
        boundary_pixel_count = 0
        min_value = math.inf
        max_value = -math.inf
        geographic_mask = [mapping(boundary)]
        with rasterio.open(temporary, "w", **profile) as destination:
            destination.set_band_description(1, "Estimated persons per grid-cell")
            destination.update_tags(
                source="WorldPop",
                dataset="2020 census/projection based population count, constrained BSGM",
                units="number of people per pixel",
                resolution="3 arc-seconds",
                nodata_meaning="Mapped as unsettled in WorldPop BSGM product",
            )
            chunk_rows = 128
            for row_offset in range(row_start, row_stop, chunk_rows):
                rows = min(chunk_rows, row_stop - row_offset)
                chunk_window = Window(col_start, row_offset, width, rows)
                values = source.read(1, window=chunk_window)
                chunk_transform = source.window_transform(chunk_window)
                inside = geometry_mask(
                    geographic_mask,
                    out_shape=values.shape,
                    transform=chunk_transform,
                    all_touched=False,
                    invert=True,
                )
                boundary_pixel_count += int(inside.sum())
                values[~inside] = nodata
                valid = inside & np.isfinite(values) & (values != nodata)
                require(
                    bool(np.all(values[valid] >= 0)),
                    "WorldPop contains negative population values other than NoData.",
                )
                if np.any(valid):
                    valid_values = values[valid]
                    valid_count += int(valid_values.size)
                    min_value = min(min_value, float(valid_values.min()))
                    max_value = max(max_value, float(valid_values.max()))
                destination.write(
                    values,
                    1,
                    window=Window(0, row_offset - row_start, width, rows),
                )
        require(valid_count > 0, "Clipped Bihar population raster has no valid data pixels.")
    temporary.replace(POPULATION_OUTPUT)

    with rasterio.open(POPULATION_OUTPUT) as output:
        bounds = output.bounds
        resolution_degrees = [abs(output.transform.a), abs(output.transform.e)]
        centroid_lon = (boundary.bounds[0] + boundary.bounds[2]) / 2
        centroid_lat = (boundary.bounds[1] + boundary.bounds[3]) / 2
        geod = Geod(ellps="WGS84")
        _, _, ew_m = geod.inv(
            centroid_lon,
            centroid_lat,
            centroid_lon + resolution_degrees[0],
            centroid_lat,
        )
        _, _, ns_m = geod.inv(
            centroid_lon,
            centroid_lat,
            centroid_lon,
            centroid_lat + resolution_degrees[1],
        )
        metadata = {
            "source": "WorldPop, University of Southampton",
            "dataset_name": "2020 census/projection-based population estimates, constrained using BSGM outputs",
            "source_url": POPULATION_URL,
            "documentation_url": WORLDPOP_PAGE,
            "dataset_doi": "10.5258/SOTON/WP00684",
            "catalog_record_region": "India",
            "catalog_product": "Constrained Individual countries 2020 (100m resolution)",
            "license": "Creative Commons Attribution 4.0 International (CC BY 4.0)",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "year": 2020,
            "population_reference_year": 2020,
            "source_file_modified_at_utc": datetime.fromtimestamp(
                POPULATION_SOURCE.stat().st_mtime, timezone.utc
            ).isoformat(),
            "source_file": str(POPULATION_SOURCE.relative_to(ROOT)),
            "source_file_bytes": POPULATION_SOURCE.stat().st_size,
            "source_file_sha256": sha256(POPULATION_SOURCE),
            "crs": output.crs.to_string(),
            "resolution": {
                "arc_seconds": [
                    resolution_degrees[0] * 3600,
                    resolution_degrees[1] * 3600,
                ],
                "degrees": resolution_degrees,
                "approximate_metres_at_bihar_centre": {
                    "east_west": ew_m,
                    "north_south": ns_m,
                },
                "source_description": "3 arc-seconds, approximately 100 m at the equator",
            },
            "units": "estimated number of people per pixel (grid-cell)",
            "raster_value_meaning": "Each cell contains the estimated total number of people in that grid-cell; values are not people per km^2.",
            "nodata": nodata,
            "nodata_semantics": "WorldPop documents NoData as areas mapped as unsettled by its Built-Settlement Growth Model (BSGM).",
            "dtype": output.dtypes[0],
            "bounds_epsg4326": [bounds.left, bounds.bottom, bounds.right, bounds.top],
            "width": output.width,
            "height": output.height,
            "band_count": output.count,
            "valid_pixel_count": valid_count,
            "population_min": min_value,
            "population_max": max_value,
            "bihar_boundary_pixel_count": boundary_pixel_count,
            "valid_pixel_percent_of_boundary_pixels": (
                valid_count / boundary_pixel_count * 100
            ),
            "boundary_spatial_coverage": {
                "source_raster_covers_bihar_boundary": True,
                "output_bounds_cover_boundary_bbox": (
                    bounds.left <= boundary.bounds[0]
                    and bounds.bottom <= boundary.bounds[1]
                    and bounds.right >= boundary.bounds[2]
                    and bounds.top >= boundary.bounds[3]
                ),
                "inside_boundary_mask_applied": True,
                "valid_pixel_count": valid_count,
                "pixel_coverage_note": "NoData cells are documented as unsettled areas, not missing values; the valid-pixel percentage is not a completeness or population-coverage estimate.",
            },
            "processing_steps": [
                "Download the actual India 2020 WorldPop constrained BSGM GeoTIFF.",
                "Verify its CRS, resolution, dtype, NoData, and Bihar extent before processing.",
                "Crop to the authoritative Bihar boundary bounding box without resampling.",
                "Mask pixels whose centres fall outside the Bihar boundary to source NoData.",
                "Preserve the native grid values, resolution, CRS, dtype, and NoData in a tiled compressed GeoTIFF.",
                "Count valid cells and calculate min/max from the clipped pixel values.",
            ],
            "output_file": str(POPULATION_OUTPUT.relative_to(ROOT)),
            "limitations": [
                "This is a modelled dasymetric gridded estimate derived from 2020 census/projection-based national inputs, not a census enumeration at each pixel.",
                "Population values are estimated people per pixel, not people per square kilometre.",
                "NoData cells denote locations modelled as unsettled; they must not be silently treated as zero population.",
                "The grid is geographic WGS84, so physical cell dimensions vary with latitude.",
            ],
        }
    POPULATION_METADATA.parent.mkdir(parents=True, exist_ok=True)
    POPULATION_METADATA.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata


def main() -> int:
    try:
        boundary, _ = load_boundary()
        boundary_transformer = Transformer.from_crs(
            STORAGE_CRS, ANALYSIS_CRS, always_xy=True
        )
        projected_boundary_area = shapely.transform(
            boundary,
            boundary_transformer.transform,
            interleaved=False,
        ).area
        if BUILDINGS_OUTPUT.is_file() and BUILDINGS_METADATA.is_file():
            with BUILDINGS_METADATA.open(encoding="utf-8") as source:
                buildings = json.load(source)
            require(
                buildings.get("synthetic_generation_used") is False
                and buildings.get("output_file") == str(BUILDINGS_OUTPUT.relative_to(ROOT))
                and buildings.get("source_archive_sha256") == sha256(OSM_ARCHIVE),
                "Existing building output metadata does not match the real OSM source; reprocess required.",
            )
            buildings["source_archive_modified_at_utc"] = datetime.fromtimestamp(
                OSM_ARCHIVE.stat().st_mtime, timezone.utc
            ).isoformat()
            BUILDINGS_METADATA.write_text(
                json.dumps(buildings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        else:
            buildings = prepare_buildings(boundary, projected_boundary_area)
        population = prepare_population(boundary)
        summary = {
            "buildings": {
                "source": buildings["source"],
                "feature_count": buildings["feature_count"],
                "crs": buildings["crs"],
                "area_km2": buildings["total_building_footprint_area_km2"],
            },
            "population": {
                "source": population["source"],
                "year": population["year"],
                "resolution": population["resolution"],
                "crs": population["crs"],
                "units": population["units"],
                "valid_pixel_count": population["valid_pixel_count"],
            },
            "status": "prepared_pending_independent_validation",
        }
        SUMMARY_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_OUTPUT.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "buildings": {
                        "features": buildings["feature_count"],
                        "valid": buildings["valid_building_features"],
                        "invalid": buildings["invalid_building_features"],
                        "repaired": buildings["repaired_building_features"],
                        "area_km2": buildings["total_building_footprint_area_km2"],
                    },
                    "population": {
                        "valid_pixels": population["valid_pixel_count"],
                        "min": population["population_min"],
                        "max": population["population_max"],
                        "crs": population["crs"],
                        "nodata": population["nodata"],
                        "bounds": population["bounds_epsg4326"],
                    },
                    "summary": str(SUMMARY_OUTPUT),
                },
                indent=2,
            )
        )
        return 0
    except Exception as error:
        print(f"STEP 2C PREPARATION FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
