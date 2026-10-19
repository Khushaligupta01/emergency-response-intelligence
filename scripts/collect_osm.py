import requests
import json
from pathlib import Path

OUTPUT_DIR = Path("data/raw/osm/bihar_flood_1104121")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

BBOX = "25.8,84.9,26.8,86.5"

query = f"""
[out:json][timeout:180];

(
  way["highway"]({BBOX});
  node["amenity"="hospital"]({BBOX});
  way["amenity"="hospital"]({BBOX});
  relation["amenity"="hospital"]({BBOX});

  node["amenity"="shelter"]({BBOX});
  way["amenity"="shelter"]({BBOX});
  relation["amenity"="shelter"]({BBOX});

  node["amenity"="school"]({BBOX});
  way["amenity"="school"]({BBOX});
  relation["amenity"="school"]({BBOX});

  way["building"]({BBOX});
  way["bridge"]({BBOX});
);

out geom;
"""

headers = {
    "User-Agent": "EmergencyResponseAI/1.0"
}

print("Collecting OSM data...")
print("This is ONE request. Please wait.")

response = requests.post(
    OVERPASS_URL,
    data=query,
    headers=headers,
    timeout=240
)

print("STATUS:", response.status_code)

response.raise_for_status()

data = response.json()

with open(
    OUTPUT_DIR / "osm_raw.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(data, f, indent=2)

metadata = {
    "source": "OpenStreetMap",
    "api": "Overpass API",
    "bbox": BBOX,
    "collection_type": "raw",
    "query_file": "osm_raw.json"
}

with open(
    OUTPUT_DIR / "metadata.json",
    "w",
    encoding="utf-8"
) as f:
    json.dump(metadata, f, indent=2)

print()
print("OSM COLLECTION COMPLETE")
print("Elements collected:", len(data.get("elements", [])))
print("Saved:", OUTPUT_DIR / "osm_raw.json")