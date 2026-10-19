"""Build and validate the GDACS episode-20 reported affected area for Bihar."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from pyproj import Transformer
from shapely import make_valid, transform as transform_geometry
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, mapping, shape
from shapely.validation import explain_validity


ROOT = Path(__file__).resolve().parents[2]
EVENT_ID = 1104121
EPISODE_ID = 20
POLYGON_TYPE = "Poly_Affected"
ANALYSIS_CRS = "EPSG:32645"
STORAGE_CRS = "EPSG:4326"
BOUNDARY_PATH = ROOT / "data/external/boundaries/bihar_boundary.geojson"
EVENT_DIR = ROOT / "data/processed/gdacs/FL_1104121"
INVESTIGATION_PATH = EVENT_DIR / "geometry_investigation.json"
OUTPUT_PATH = EVENT_DIR / "bihar_flood_extent.geojson"
METADATA_PATH = EVENT_DIR / "bihar_flood_extent_metadata.json"
SUMMARY_PATH = EVENT_DIR / "bihar_flood_extent_summary.json"
SOURCE_URL = (
    "https://www.gdacs.org/gdacsapi/api/polygons/getgeometry"
    "?eventtype=FL&eventid=1104121&episodeid=20"
)
BOUNDARY_NAME = "Bihar"
CONTAINMENT_INSET_DEGREES = 1e-10
BOUNDARY_SOURCE_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
    "releaseData/gbOpen/IND/ADM1/geoBoundaries-IND-ADM1.geojson"
)
LIMITATION = (
    "This dataset represents a GDACS analyst-reported affected area and "
    "must not be interpreted as an observed satellite-derived inundation "
    "extent."
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def fetch_source_geometry() -> tuple[dict, object]:
    request = Request(
        SOURCE_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "EmergencyResponseAI/1.0",
        },
    )
    with urlopen(request, timeout=120) as response:
        require(response.status == 200, f"GDACS returned HTTP {response.status}.")
        document = json.load(response)

    require(
        document.get("type") == "FeatureCollection",
        "Episode 20 GDACS geometry response is not a FeatureCollection.",
    )
    candidates = []
    for feature in document.get("features", []):
        properties = feature.get("properties") or {}
        if (
            str(properties.get("eventtype", "")).upper() == "FL"
            and int(properties.get("eventid", -1)) == EVENT_ID
            and int(properties.get("episodeid", -1)) == EPISODE_ID
            and properties.get("Class") == POLYGON_TYPE
        ):
            candidates.append(feature)
    require(
        len(candidates) == 1,
        "Expected exactly one event 1104121, episode 20, Poly_Affected "
        f"feature; found {len(candidates)}.",
    )
    feature = candidates[0]
    require(feature.get("geometry") is not None, "Selected GDACS feature has no geometry.")
    return feature, shape(feature["geometry"])


def polygonal_parts(geometry) -> tuple[list[Polygon], list[str]]:
    polygons = []
    non_polygon_types = []
    if isinstance(geometry, Polygon):
        polygons.append(geometry)
    elif isinstance(geometry, MultiPolygon):
        polygons.extend(geometry.geoms)
    elif isinstance(geometry, GeometryCollection):
        for part in geometry.geoms:
            child_polygons, child_non_polygon_types = polygonal_parts(part)
            polygons.extend(child_polygons)
            non_polygon_types.extend(child_non_polygon_types)
    else:
        non_polygon_types.append(geometry.geom_type)
    return polygons, non_polygon_types


def load_bihar_boundary():
    require(BOUNDARY_PATH.is_file(), f"Bihar boundary file does not exist: {BOUNDARY_PATH}")
    document = read_json(BOUNDARY_PATH)
    require(document.get("type") == "FeatureCollection", "Bihar boundary is not a FeatureCollection.")
    features = document.get("features", [])
    require(len(features) == 1, f"Expected one Bihar boundary feature, found {len(features)}.")
    require(features[0].get("geometry") is not None, "Bihar boundary feature has no geometry.")
    boundary = shape(features[0]["geometry"])
    require(not boundary.is_empty, "Bihar boundary geometry is empty.")
    require(boundary.is_valid, f"Bihar boundary is invalid: {explain_validity(boundary)}")
    require(
        boundary.geom_type in ("Polygon", "MultiPolygon"),
        f"Bihar boundary is not polygonal: {boundary.geom_type}.",
    )
    return boundary, document


def validate_saved_extent(boundary, transformer) -> tuple[dict, object]:
    require(OUTPUT_PATH.is_file(), f"Output file does not exist: {OUTPUT_PATH}")
    try:
        document = read_json(OUTPUT_PATH)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Output is not valid JSON: {error}") from error

    require(
        document.get("type") == "FeatureCollection",
        "Output GeoJSON root type is not FeatureCollection.",
    )
    require(
        document.get("coordinate_reference_system") == STORAGE_CRS,
        f"Output CRS must be {STORAGE_CRS}.",
    )
    features = document.get("features")
    require(isinstance(features, list) and len(features) == 1, "Output must contain exactly one feature.")
    feature = features[0]
    properties = feature.get("properties") or {}
    source_properties = properties.get("source_feature_properties") or {}
    require(int(properties.get("event_id", -1)) == EVENT_ID, "Output event ID is not 1104121.")
    require(int(properties.get("episode_id", -1)) == EPISODE_ID, "Output episode ID is not 20.")
    require(properties.get("polygon_type") == POLYGON_TYPE, "Output polygon type is not Poly_Affected.")
    require(properties.get("polygon_type") != "Poly_Global", "Poly_Global must not contribute to output.")
    require(properties.get("source_url") == SOURCE_URL, "Output source URL is not the selected episode 20 endpoint.")
    require(
        int(source_properties.get("eventid", -1)) == EVENT_ID
        and str(source_properties.get("eventtype", "")).upper() == "FL",
        "Original GDACS feature properties do not identify event 1104121 (FL).",
    )
    require(
        int(source_properties.get("episodeid", -1)) == EPISODE_ID,
        "Original GDACS feature properties do not identify episode 20.",
    )
    require(
        source_properties.get("Class") == POLYGON_TYPE,
        "Original GDACS feature is not Poly_Affected.",
    )
    require(
        source_properties.get("Class") != "Poly_Global",
        "Original GDACS Poly_Global geometry must not contribute to output.",
    )
    require(
        properties.get("geometry_semantics") == "GDACS analyst-reported affected area",
        "Output does not identify the GDACS reported affected-area semantics.",
    )
    require(properties.get("not_observed_inundation") is True, "Output must disclaim observed inundation.")
    require(feature.get("geometry") is not None, "Output feature has no geometry.")

    geometry = shape(feature["geometry"])
    require(not geometry.is_empty, "Output geometry is empty.")
    require(geometry.geom_type in ("Polygon", "MultiPolygon"), "Output geometry is not polygonal.")
    require(geometry.is_valid, f"Output geometry is invalid: {explain_validity(geometry)}")
    require(geometry.intersects(boundary), "Output geometry does not intersect Bihar.")
    difference = geometry.difference(boundary)
    require(difference.is_empty, "Output geometry is not completely contained within Bihar.")

    projected_geometry = transform_geometry(
        geometry, transformer.transform, interleaved=False
    )
    projected_boundary = transform_geometry(
        boundary, transformer.transform, interleaved=False
    )
    area_km2 = projected_geometry.area / 1_000_000
    boundary_area_km2 = projected_boundary.area / 1_000_000
    require(area_km2 > 0, "Projected affected area must be positive.")
    require(boundary_area_km2 > 0, "Projected Bihar boundary area must be positive.")
    require(properties.get("area_calculation_crs") == ANALYSIS_CRS, "Area calculation CRS is not projected.")
    require(
        properties.get("selected_episode") == EPISODE_ID,
        "Output does not explicitly record selected episode 20.",
    )
    require(
        properties.get("selected_polygon_type") == POLYGON_TYPE,
        "Output does not explicitly record Poly_Affected.",
    )
    component_count = len(geometry.geoms) if geometry.geom_type == "MultiPolygon" else 1
    return (
        {
            "valid": geometry.is_valid,
            "non_empty": not geometry.is_empty,
            "intersects_bihar": geometry.intersects(boundary),
            "inside_bihar": difference.is_empty,
            "positive_area": area_km2 > 0,
            "area_calculated_in_projected_crs": True,
            "no_poly_global_used": properties["polygon_type"] == POLYGON_TYPE,
            "selected_episode_is_20": properties["episode_id"] == EPISODE_ID,
            "selected_polygon_type_is_poly_affected": properties["polygon_type"] == POLYGON_TYPE,
            "feature_collection": document["type"] == "FeatureCollection",
            "crs_epsg4326": document["coordinate_reference_system"] == STORAGE_CRS,
            "polygonal_geometry": geometry.geom_type in ("Polygon", "MultiPolygon"),
            "file_exists": OUTPUT_PATH.is_file(),
            "geometry_type": geometry.geom_type,
            "polygon_component_count": component_count,
            "area_km2": area_km2,
            "bihar_area_km2": boundary_area_km2,
            "coverage_percent": area_km2 / boundary_area_km2 * 100,
            "crs": STORAGE_CRS,
        },
        geometry,
    )


def finalize() -> dict:
    require(INVESTIGATION_PATH.is_file(), f"Investigation file is missing: {INVESTIGATION_PATH}")
    investigation = read_json(INVESTIGATION_PATH)
    event = investigation.get("event") or {}
    selected = investigation.get("selected_candidate") or {}
    require(str(event.get("event_id")) == str(EVENT_ID), "Investigation event ID is not 1104121.")
    require(int(selected.get("episode_id", -1)) == EPISODE_ID, "Investigation selected episode is not 20.")
    require(selected.get("polygon_type") == POLYGON_TYPE, "Investigation selected type is not Poly_Affected.")

    boundary, boundary_document = load_bihar_boundary()
    original_feature, original_geometry = fetch_source_geometry()
    original_properties = original_feature.get("properties") or {}
    original_valid = original_geometry.is_valid
    original_type = original_geometry.geom_type
    original_reason = "Valid Geometry" if original_valid else explain_validity(original_geometry)

    repaired_geometry = original_geometry if original_valid else make_valid(original_geometry)
    source_polygons, source_non_polygon_types = polygonal_parts(repaired_geometry)
    require(source_polygons, "Source geometry has no polygonal components after validation/repair.")
    source_polygon_geometry = (
        source_polygons[0] if len(source_polygons) == 1 else MultiPolygon(source_polygons)
    )
    require(
        source_polygon_geometry.is_valid,
        "Polygonal source geometry remains invalid after repair: "
        f"{explain_validity(source_polygon_geometry)}",
    )

    boundary_inset = boundary.buffer(-CONTAINMENT_INSET_DEGREES)
    require(
        not boundary_inset.is_empty and boundary_inset.is_valid,
        "Bihar boundary precision inset is empty or invalid.",
    )
    raw_intersection = source_polygon_geometry.intersection(boundary)
    clipped = source_polygon_geometry.intersection(boundary_inset)
    clipped_polygons, clipped_non_polygon_types = polygonal_parts(clipped)
    require(clipped_polygons, "Episode 20 Poly_Affected has no polygonal Bihar intersection.")
    final_geometry = (
        clipped_polygons[0]
        if len(clipped_polygons) == 1
        else MultiPolygon(clipped_polygons)
    )
    require(final_geometry.is_valid, f"Clipped geometry is invalid: {explain_validity(final_geometry)}")
    require(not final_geometry.is_empty, "Clipped geometry is empty.")

    boundary_crs = boundary_document.get("crs", {}).get("properties", {}).get("name")
    require(
        boundary_crs in (None, "urn:ogc:def:crs:OGC:1.3:CRS84"),
        f"Unexpected Bihar boundary CRS: {boundary_crs}.",
    )
    transformer = Transformer.from_crs(STORAGE_CRS, ANALYSIS_CRS, always_xy=True)
    source_area = (
        transform_geometry(
            original_geometry, transformer.transform, interleaved=False
        ).area
        / 1_000_000
    )

    source_bbox = list(original_geometry.bounds)
    source_component_count = len(original_geometry.geoms) if original_geometry.geom_type == "MultiPolygon" else (
        1 if original_geometry.geom_type == "Polygon" else 0
    )
    output_feature = {
        "type": "Feature",
        "properties": {
            "source": "GDACS",
            "event_id": EVENT_ID,
            "event_type": "FL",
            "episode_id": EPISODE_ID,
            "selected_episode": EPISODE_ID,
            "polygon_type": POLYGON_TYPE,
            "selected_polygon_type": POLYGON_TYPE,
            "source_url": SOURCE_URL,
            "source_feature_properties": original_properties,
            "source_geometry_type_before_repair": original_type,
            "source_geometry_valid_before_repair": original_valid,
            "source_geometry_validity_reason": original_reason,
            "source_geometry_bbox_epsg4326": source_bbox,
            "source_geometry_polygon_component_count": source_component_count,
            "source_geometry_area_km2_epsg32645": source_area,
            "source_geometry_repaired": not original_valid,
            "source_geometry_type_after_repair": repaired_geometry.geom_type,
            "source_non_polygonal_parts_excluded": source_non_polygon_types,
            "clip_non_polygonal_parts_excluded": clipped_non_polygon_types,
            "geometry_semantics": "GDACS analyst-reported affected area",
            "not_observed_inundation": True,
            "boundary": BOUNDARY_NAME,
            "boundary_source": "geoBoundaries gbOpen India ADM1",
            "boundary_source_url": BOUNDARY_SOURCE_URL,
            "area_calculation_crs": ANALYSIS_CRS,
            "containment_inset_degrees": CONTAINMENT_INSET_DEGREES,
        },
        "geometry": mapping(final_geometry),
    }
    output_document = {
        "type": "FeatureCollection",
        "name": "GDACS reported affected area for Bihar",
        "coordinate_reference_system": STORAGE_CRS,
        "features": [output_feature],
    }
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as output:
        json.dump(output_document, output, ensure_ascii=False, indent=2)

    validation, saved_geometry = validate_saved_extent(boundary, transformer)
    require(
        all(
            validation[key]
            for key in (
                "file_exists",
                "feature_collection",
                "crs_epsg4326",
                "polygonal_geometry",
                "valid",
                "non_empty",
                "intersects_bihar",
                "inside_bihar",
                "positive_area",
                "area_calculated_in_projected_crs",
                "no_poly_global_used",
                "selected_episode_is_20",
                "selected_polygon_type_is_poly_affected",
            )
        ),
        "One or more required validation checks failed.",
    )

    metadata = read_json(METADATA_PATH) if METADATA_PATH.is_file() else {}
    metadata.update(
        {
            "event_id": EVENT_ID,
            "event_type": "FL",
            "selected_event_id": str(EVENT_ID),
            "selected_episode": EPISODE_ID,
            "selected_episode_id": EPISODE_ID,
            "selected_polygon_type": POLYGON_TYPE,
            "geometry_semantics": "GDACS analyst-reported affected area",
            "not_observed_inundation": True,
            "source": "GDACS",
            "boundary": BOUNDARY_NAME,
            "boundary_source": "geoBoundaries gbOpen India ADM1",
            "boundary_source_url": BOUNDARY_SOURCE_URL,
            "crs": STORAGE_CRS,
            "storage_crs": STORAGE_CRS,
            "output_file": OUTPUT_PATH.name,
            "area_km2": validation["area_km2"],
            "final_area_km2": validation["area_km2"],
            "bihar_area_km2": validation["bihar_area_km2"],
            "coverage_percent": validation["coverage_percent"],
            "polygon_component_count": validation["polygon_component_count"],
            "final_geometry_type": validation["geometry_type"],
            "area_calculation_crs": ANALYSIS_CRS,
            "processing_status": "complete_analyst_reported_affected_area_not_observed_inundation",
            "source_geometry": {
                "endpoint": SOURCE_URL,
                "event_id": EVENT_ID,
                "event_type": "FL",
                "episode_id": EPISODE_ID,
                "polygon_type": POLYGON_TYPE,
                "geometry_type_before_repair": original_type,
                "valid_before_repair": original_valid,
                "validity_reason_before_repair": original_reason,
                "geometry_repaired": not original_valid,
                "geometry_type_after_repair": repaired_geometry.geom_type,
                "polygon_component_count_before_clip": len(source_polygons),
                "source_polygonal_area_km2": source_area,
                "source_bbox_epsg4326": source_bbox,
                "source_non_polygonal_parts_excluded": source_non_polygon_types,
            },
            "clipping": {
                "operation": "Episode 20 Poly_Affected INTERSECTION Bihar boundary",
                "raw_intersection_area_km2": transform_geometry(
                    raw_intersection, transformer.transform, interleaved=False
                ).area
                / 1_000_000,
                "containment_precision_inset_degrees": CONTAINMENT_INSET_DEGREES,
                "non_polygonal_intersection_parts_excluded": clipped_non_polygon_types,
                "poly_global_used": False,
            },
            "validation": validation,
            "blocker": None,
            "processing_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        }
    )
    with METADATA_PATH.open("w", encoding="utf-8") as output:
        json.dump(metadata, output, ensure_ascii=False, indent=2)

    summary = {
        "event": {
            "event_id": EVENT_ID,
            "event_type": "FL",
            "source": "GDACS",
        },
        "selected_episode": EPISODE_ID,
        "selected_polygon_type": POLYGON_TYPE,
        "source": SOURCE_URL,
        "boundary": {
            "name": BOUNDARY_NAME,
            "source": "geoBoundaries gbOpen India ADM1",
            "source_url": BOUNDARY_SOURCE_URL,
            "crs": STORAGE_CRS,
            "area_km2": validation["bihar_area_km2"],
        },
        "semantic_meaning": "GDACS analyst-reported affected area",
        "not_observed_inundation": True,
        "output_file": OUTPUT_PATH.name,
        "crs": STORAGE_CRS,
        "area_km2": validation["area_km2"],
        "bihar_area_km2": validation["bihar_area_km2"],
        "coverage_percent": validation["coverage_percent"],
        "geometry_type": validation["geometry_type"],
        "polygon_component_count": validation["polygon_component_count"],
        "validation": validation,
        "limitations": [
            LIMITATION,
            "The GDACS analyst-reported area is not evidence that every location in the polygon was inundated.",
            "The separate GDACS-linked GFM Observed product did not provide a usable Bihar observed-flood geometry.",
            "The GDACS episode geometry was clipped with a 1e-10 degree inward boundary tolerance to ensure strict containment.",
        ],
    }
    with SUMMARY_PATH.open("w", encoding="utf-8") as output:
        json.dump(summary, output, ensure_ascii=False, indent=2)

    require(METADATA_PATH.is_file(), "Updated metadata file was not written.")
    require(SUMMARY_PATH.is_file(), "Summary file was not written.")
    require(saved_geometry.is_valid and not saved_geometry.is_empty, "Final saved geometry failed validation.")
    return validation


def main() -> int:
    try:
        validation = finalize()
    except Exception as error:
        print("STEP 2B BLOCKED")
        print(f"Failure: {error}")
        return 1

    print("STEP 2B FINALIZATION")
    print(f"Event: {EVENT_ID}")
    print(f"Episode: {EPISODE_ID}")
    print(f"Polygon type: {POLYGON_TYPE}")
    print(f"Bihar boundary: {BOUNDARY_PATH.relative_to(ROOT)}")
    print(f"Output: {OUTPUT_PATH.relative_to(ROOT)}")
    print(f"Geometry type: {validation['geometry_type']}")
    print(f"Components: {validation['polygon_component_count']}")
    print(f"Area: {validation['area_km2']:.6f} km^2 (GDACS reported affected area)")
    print(f"Bihar area: {validation['bihar_area_km2']:.6f} km^2")
    print(f"Coverage: {validation['coverage_percent']:.6f}%")
    print(f"CRS: {validation['crs']}")
    print(f"Valid: {validation['valid']}")
    print(f"Inside Bihar: {validation['inside_bihar']}")
    print(f"Non-empty: {validation['non_empty']}")
    print("STEP 2B COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
