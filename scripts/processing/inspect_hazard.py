import json
from pathlib import Path

from shapely.geometry import shape, mapping
from shapely.validation import make_valid
from shapely.ops import unary_union


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

INPUT_FILE = Path(
    "data/raw/gdacs/FL_1104121/observed_flood.geojson"
)

OUTPUT_DIR = Path("data/processed/gdacs/FL_1104121")
OUTPUT_FILE = OUTPUT_DIR / "flood_extent_cleaned.geojson"


# ---------------------------------------------------------
# Load GeoJSON
# ---------------------------------------------------------

print("=" * 65)
print("GDACS FLOOD GEOMETRY PREPARATION")
print("=" * 65)

print(f"\nInput file:")
print(f"  {INPUT_FILE}")

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

features = data.get("features", [])

print(f"\nOriginal features: {len(features)}")


# ---------------------------------------------------------
# Convert + repair geometries
# ---------------------------------------------------------

valid_geometries = []

for i, feature in enumerate(features, start=1):

    geometry_data = feature.get("geometry")

    if not geometry_data:
        print(f"Feature {i}: no geometry -> skipped")
        continue

    geom = shape(geometry_data)

    print(
        f"Feature {i}: "
        f"{geom.geom_type}, "
        f"valid={geom.is_valid}"
    )

    # Repair invalid geometry
    if not geom.is_valid:
        geom = make_valid(geom)

        print(
            f"           repaired -> "
            f"{geom.geom_type}, "
            f"valid={geom.is_valid}"
        )

    if geom.is_empty:
        print("           empty geometry -> skipped")
        continue

    valid_geometries.append(geom)


# ---------------------------------------------------------
# Keep polygonal flood geometry
# ---------------------------------------------------------

polygon_geometries = []

for geom in valid_geometries:

    if geom.geom_type in ("Polygon", "MultiPolygon"):
        polygon_geometries.append(geom)

    elif geom.geom_type == "GeometryCollection":

        for part in geom.geoms:

            if part.geom_type in ("Polygon", "MultiPolygon"):
                polygon_geometries.append(part)


print(
    f"\nPolygon geometries available: "
    f"{len(polygon_geometries)}"
)


if not polygon_geometries:
    raise RuntimeError(
        "No polygon geometry was found in the GDACS flood data."
    )


# ---------------------------------------------------------
# Merge flood polygons
# ---------------------------------------------------------

print("\nMerging flood polygons...")

flood_extent = unary_union(polygon_geometries)

print(f"Final geometry type: {flood_extent.geom_type}")
print(f"Final geometry valid: {flood_extent.is_valid}")
print(f"Final geometry empty: {flood_extent.is_empty}")


# ---------------------------------------------------------
# Calculate bounding box
# ---------------------------------------------------------

min_lon, min_lat, max_lon, max_lat = flood_extent.bounds

print("\nFLOOD EXTENT BOUNDING BOX")
print("-" * 65)

print(f"Minimum longitude : {min_lon}")
print(f"Minimum latitude  : {min_lat}")
print(f"Maximum longitude : {max_lon}")
print(f"Maximum latitude  : {max_lat}")


# ---------------------------------------------------------
# Save cleaned geometry
# ---------------------------------------------------------

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

output_geojson = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "source": "GDACS",
                "event_id": "1104121",
                "hazard_type": "flood",
                "geometry_status": "validated_and_repaired",
            },
            "geometry": mapping(flood_extent),
        }
    ],
}

with open(
    OUTPUT_FILE,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        output_geojson,
        f,
        indent=2
    )


print("\nCLEANED FLOOD GEOMETRY SAVED")
print("-" * 65)
print(OUTPUT_FILE)

print("\n" + "=" * 65)
print("STEP 2A COMPLETE")
print("=" * 65)