"""Clip the GDACS flood geometry to Bihar using the geoBoundaries ADM1 layer."""

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import geopandas as gpd
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.ops import unary_union


ROOT = Path(__file__).resolve().parents[2]
EVENT_ID = "1104121"
ANALYSIS_CRS = "EPSG:32645"
BOUNDARY_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
    "releaseData/gbOpen/IND/ADM1/geoBoundaries-IND-ADM1.geojson"
)
BOUNDARY_PATH = ROOT / "data/external/boundaries/india_adm1.geojson"
INPUT_PATH = ROOT / "data/raw/gdacs/FL_1104121/observed_flood.geojson"
OUTPUT_DIR = ROOT / "data/processed/gdacs/FL_1104121"
OUTPUT_PATH = OUTPUT_DIR / "bihar_flood_extent.geojson"
SUMMARY_PATH = OUTPUT_DIR / "bihar_flood_extent_metadata.json"
BIHAR_BOUNDARY_PATH = (
    ROOT / "data/external/boundaries/bihar_boundary.geojson"
)


def polygon_parts(geometry):
    """Return polygon components from a possibly repaired geometry."""
    geometry = make_valid(geometry)
    if isinstance(geometry, Polygon):
        return [geometry]
    if isinstance(geometry, MultiPolygon):
        return list(geometry.geoms)
    if isinstance(geometry, GeometryCollection):
        return [
            part
            for child in geometry.geoms
            for part in polygon_parts(child)
        ]
    return []


def ensure_boundary() -> None:
    if BOUNDARY_PATH.exists():
        return
    BOUNDARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        BOUNDARY_URL,
        headers={"User-Agent": "EmergencyResponseAI/1.0"},
    )
    with urlopen(request, timeout=120) as response:
        BOUNDARY_PATH.write_bytes(response.read())


def load_bihar_boundary() -> gpd.GeoDataFrame:
    ensure_boundary()
    boundaries = gpd.read_file(BOUNDARY_PATH)
    if boundaries.empty or boundaries.crs is None:
        raise ValueError("Downloaded India ADM1 boundary has no features or CRS.")

    if "shapeISO" in boundaries.columns:
        bihar = boundaries[
            boundaries["shapeISO"].astype(str).str.upper() == "IN-BR"
        ].copy()
    else:
        name_fields = ("shapeName", "shape_name", "name", "NAME_1", "state")
        name_field = next(
            (field for field in name_fields if field in boundaries.columns),
            None,
        )
        if name_field is None:
            raise ValueError(
                f"Cannot identify state names in boundary fields: "
                f"{list(boundaries.columns)}"
            )
        names = boundaries[name_field].astype(str).str.strip().str.casefold()
        bihar = boundaries[names.isin(("bihar", "bihār"))].copy()
    if len(bihar) != 1:
        raise ValueError(f"Expected one Bihar ADM1 feature, found {len(bihar)}.")

    bihar = bihar.to_crs("EPSG:4326")
    repaired = [part for geom in bihar.geometry for part in polygon_parts(geom)]
    if not repaired:
        raise ValueError("Bihar boundary contains no polygon geometry.")
    boundary = unary_union(repaired)
    if boundary.is_empty or not boundary.is_valid:
        raise ValueError("Bihar boundary is empty or invalid after repair.")
    return gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")


def prepare_extent() -> dict:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(f"Raw GDACS event geometry not found: {INPUT_PATH}")
    with INPUT_PATH.open(encoding="utf-8") as source:
        event = json.load(source)
    original_features = event.get("features", [])
    affected_features = [
        feature
        for feature in original_features
        if str(feature.get("properties", {}).get("Class", "")).casefold()
        == "poly_affected"
    ]
    if not affected_features:
        raise ValueError(
            "The GDACS event has no feature classified as Poly_Affected."
        )

    boundary = load_bihar_boundary()
    boundary_projected = boundary.to_crs(ANALYSIS_CRS).geometry.iloc[0]
    BIHAR_BOUNDARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    boundary.to_file(BIHAR_BOUNDARY_PATH, driver="GeoJSON")

    flood_frame = gpd.GeoDataFrame(
        geometry=[
            shape(feature["geometry"])
            for feature in affected_features
            if feature.get("geometry")
        ],
        crs="EPSG:4326",
    ).to_crs(ANALYSIS_CRS)

    flood_polygons = [
        part
        for geometry in flood_frame.geometry
        for part in polygon_parts(geometry)
    ]
    if not flood_polygons:
        raise ValueError("The cleaned GDACS data contains no polygon geometry.")

    clipped = []
    intersecting_count = 0
    for polygon in flood_polygons:
        intersection = polygon.intersection(boundary_projected)
        parts = polygon_parts(intersection) if not intersection.is_empty else []
        if parts and sum(part.area for part in parts) > 0:
            intersecting_count += 1
            clipped.extend(parts)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if clipped:
        final_geometry = unary_union(clipped)
        final_geometry = unary_union(polygon_parts(final_geometry))
        if (
            final_geometry.is_empty
            or not final_geometry.is_valid
            or final_geometry.geom_type not in ("Polygon", "MultiPolygon")
        ):
            raise ValueError("Final Bihar flood geometry failed validity checks.")
        outside_area = final_geometry.difference(boundary_projected).area
        if outside_area > max(final_geometry.area, 1) * 1e-10:
            raise ValueError("Clipped flood geometry extends outside Bihar.")
        area_km2 = final_geometry.area / 1_000_000
        output_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "event_id": EVENT_ID,
                        "source": "GDACS",
                        "disaster_type": "flood",
                        "study_area": "Bihar",
                        "analysis_crs": ANALYSIS_CRS,
                    },
                    "geometry": gpd.GeoSeries(
                        [final_geometry], crs=ANALYSIS_CRS
                    ).to_crs("EPSG:4326").iloc[0].__geo_interface__,
                }
            ],
        }
        with OUTPUT_PATH.open("w", encoding="utf-8") as output:
            json.dump(output_geojson, output, indent=2)
    else:
        final_geometry = None
        area_km2 = None

    metadata = {
        "event_id": EVENT_ID,
        "source": "GDACS",
        "disaster_type": "flood",
        "study_area": "Bihar",
        "processing_status": (
            "complete" if final_geometry is not None else "blocked_no_affected_area_overlap"
        ),
        "boundary_source": "geoBoundaries gbOpen India ADM1",
        "boundary_source_url": BOUNDARY_URL,
        "boundary_license": "CC BY 2.5 IN",
        "boundary_feature_count": len(boundary),
        "boundary_crs": boundary.crs.to_string(),
        "boundary_geometry_type": boundary.geometry.iloc[0].geom_type,
        "boundary_valid": bool(boundary.geometry.iloc[0].is_valid),
        "boundary_bounds": list(boundary.total_bounds),
        "original_feature_count": len(original_features),
        "affected_source_feature_count": len(affected_features),
        "excluded_non_affected_feature_count": (
            len(original_features) - len(affected_features)
        ),
        "polygons_after_cleaning": len(flood_polygons),
        "polygons_intersecting_bihar": intersecting_count,
        "final_geometry_type": (
            final_geometry.geom_type if final_geometry is not None else None
        ),
        "final_area_km2": area_km2,
        "area_calculation_crs": ANALYSIS_CRS,
        "storage_crs": "EPSG:4326",
        "final_geometry_valid": (
            bool(final_geometry.is_valid) if final_geometry is not None else None
        ),
        "final_geometry_empty": (
            bool(final_geometry.is_empty) if final_geometry is not None else None
        ),
        "blocker": (
            None
            if final_geometry is not None
            else "GDACS Poly_Affected geometry has no positive-area overlap "
            "with Bihar. The intersecting Poly_Global feature is not a "
            "flood-affected area and is excluded."
        ),
        "processing_timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    with SUMMARY_PATH.open("w", encoding="utf-8") as output:
        json.dump(metadata, output, indent=2)

    print("STEP 2B - BIHAR FLOOD EXTENT")
    print("--------------------------------")
    print(f"GDACS event: {EVENT_ID}")
    print(f"Original features: {len(original_features)}")
    print(f"Affected source features: {len(affected_features)}")
    print(f"Valid flood polygons: {len(flood_polygons)}")
    print(f"Bihar-intersecting polygons: {intersecting_count}")
    if final_geometry is not None:
        print(f"Final geometry: {final_geometry.geom_type}")
        print(f"Final flood area: {area_km2:,.2f} km² ({ANALYSIS_CRS})")
        print(f"Valid: {final_geometry.is_valid}")
        print(f"Empty: {final_geometry.is_empty}")
        print(f"Saved: {OUTPUT_PATH}")
    else:
        print("No valid Bihar flood extent was created.")
        print("STEP 2B BLOCKED: GDACS affected-area polygons do not overlap Bihar.")
    print(f"Metadata: {SUMMARY_PATH}")
    if final_geometry is None:
        raise RuntimeError(metadata["blocker"])
    return metadata


if __name__ == "__main__":
    prepare_extent()
