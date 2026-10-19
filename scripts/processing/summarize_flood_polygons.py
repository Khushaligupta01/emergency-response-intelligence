import json
from pathlib import Path

from shapely.geometry import shape
from pyproj import Geod


INPUT_FILE = Path(
    "data/processed/gdacs/FL_1104121/flood_extent_cleaned.geojson"
)

OUTPUT_FILE = Path(
    "data/processed/gdacs/FL_1104121/flood_polygon_summary.csv"
)

geod = Geod(ellps="WGS84")


with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)


geometry = shape(data["features"][0]["geometry"])


print("=" * 80)
print("GDACS FLOOD POLYGON SUMMARY")
print("=" * 80)

print(f"\nTotal polygons: {len(geometry.geoms)}")


rows = []


for polygon_id, polygon in enumerate(geometry.geoms, start=1):

    min_lon, min_lat, max_lon, max_lat = polygon.bounds

    centroid = polygon.centroid

    area_m2, _ = geod.geometry_area_perimeter(polygon)

    area_km2 = abs(area_m2) / 1_000_000

    rows.append(
        {
            "polygon_id": polygon_id,
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
            "centroid_lon": centroid.x,
            "centroid_lat": centroid.y,
            "area_km2": area_km2,
        }
    )


rows.sort(
    key=lambda x: x["area_km2"],
    reverse=True
)


print("\nLargest flood polygons:")
print("-" * 80)

for row in rows[:15]:

    print(
        f"Polygon {row['polygon_id']:>2} | "
        f"Area: {row['area_km2']:>12,.2f} km² | "
        f"Centroid: "
        f"({row['centroid_lon']:.4f}, "
        f"{row['centroid_lat']:.4f})"
    )


# ---------------------------------------------------------
# Save CSV
# ---------------------------------------------------------

OUTPUT_FILE.parent.mkdir(
    parents=True,
    exist_ok=True
)


with open(
    OUTPUT_FILE,
    "w",
    encoding="utf-8",
    newline=""
) as f:

    import csv

    writer = csv.DictWriter(
        f,
        fieldnames=rows[0].keys()
    )

    writer.writeheader()
    writer.writerows(rows)


print("\nFull summary saved to:")
print(OUTPUT_FILE)

print("\n" + "=" * 80)
print("STEP 2B COMPLETE")
print("=" * 80)