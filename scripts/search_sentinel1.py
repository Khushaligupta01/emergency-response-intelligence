import json
from datetime import datetime
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION
# ============================================================

STAC_URL = "https://stac.dataspace.copernicus.eu/v1/search"

EVENT_NAME = "bihar_flood_1104121"

# Bihar study area
BBOX = [84.9, 25.8, 86.5, 26.8]

# GDACS event dates
EVENT_START = "2026-08-09T00:00:00Z"
EVENT_END = "2026-10-07T23:59:59Z"

# We search a reasonable period BEFORE the disaster
BASELINE_START = "2026-07-01T00:00:00Z"
BASELINE_END = "2026-08-08T23:59:59Z"

# Desired number of useful acquisitions
MAX_BASELINE_SCENES = 2
MAX_EVENT_SCENES = 5


# ============================================================
# OUTPUT DIRECTORIES
# ============================================================

OUTPUT_DIR = (
    Path("data")
    / "raw"
    / "sentinel"
    / "sentinel1"
    / EVENT_NAME
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# SEARCH FUNCTION
# ============================================================

def search_sentinel(start_date, end_date):

    payload = {
        "collections": ["sentinel-1-grd"],
        "bbox": BBOX,
        "datetime": f"{start_date}/{end_date}",
        "query": {
            "product:type": {
                "eq": "IW_GRDH_1S"
            },
            "sar:polarizations": {
                "eq": ["VV", "VH"]
            }
        },
        "limit": 100
    }

    response = requests.post(
        STAC_URL,
        json=payload,
        timeout=60
    )

    response.raise_for_status()

    return response.json().get("features", [])


# ============================================================
# BBOX OVERLAP
# ============================================================

def bbox_overlap_score(product_bbox):

    if not product_bbox or len(product_bbox) != 4:
        return 0.0

    min_lon, min_lat, max_lon, max_lat = product_bbox

    study_min_lon, study_min_lat, study_max_lon, study_max_lat = BBOX

    overlap_min_lon = max(min_lon, study_min_lon)
    overlap_max_lon = min(max_lon, study_max_lon)

    overlap_min_lat = max(min_lat, study_min_lat)
    overlap_max_lat = min(max_lat, study_max_lat)

    if (
        overlap_max_lon <= overlap_min_lon
        or overlap_max_lat <= overlap_min_lat
    ):
        return 0.0

    overlap_area = (
        overlap_max_lon - overlap_min_lon
    ) * (
        overlap_max_lat - overlap_min_lat
    )

    study_area = (
        study_max_lon - study_min_lon
    ) * (
        study_max_lat - study_min_lat
    )

    return overlap_area / study_area


# ============================================================
# EXTRACT PRODUCT METADATA
# ============================================================

def extract_product(item):

    properties = item.get("properties", {})

    bbox = item.get("bbox")

    datetime_value = properties.get("datetime")

    if datetime_value:
        acquisition_date = datetime_value[:10]
    else:
        acquisition_date = None

    return {
        "product_id": item.get("id"),
        "datetime": datetime_value,
        "acquisition_date": acquisition_date,
        "platform": properties.get("platform"),
        "orbit": properties.get("sat:orbit_state"),
        "relative_orbit": properties.get("sat:relative_orbit"),
        "mode": properties.get("sar:instrument_mode"),
        "product_type": properties.get("product:type"),
        "polarization": properties.get("sar:polarizations"),
        "bbox": bbox,
        "bbox_overlap_score": round(
            bbox_overlap_score(bbox), 4
        ),
        "assets": item.get("assets", {})
    }


# ============================================================
# REMOVE SAME-DAY / SAME-ORBIT DUPLICATE SLICES
# ============================================================

def remove_duplicate_acquisitions(products):

    groups = {}

    for product in products:

        key = (
            product["acquisition_date"],
            product["orbit"],
            product["relative_orbit"]
        )

        groups.setdefault(key, []).append(product)

    selected = []

    for _, group in groups.items():

        # Select the slice covering the largest portion
        # of the study area.
        best = max(
            group,
            key=lambda x: x["bbox_overlap_score"]
        )

        selected.append(best)

    selected.sort(
        key=lambda x: x["datetime"]
    )

    return selected


# ============================================================
# SELECT EVENT SCENES
# ============================================================

def select_event_scenes(products):

    products = remove_duplicate_acquisitions(products)

    # Prefer products with good study-area coverage.
    products.sort(
        key=lambda x: (
            x["bbox_overlap_score"],
            x["datetime"]
        ),
        reverse=True
    )

    selected = []

    for product in products:

        if product["bbox_overlap_score"] <= 0:
            continue

        # Avoid selecting many scenes only a few days apart.
        date = product["acquisition_date"]

        too_close = False

        for existing in selected:

            existing_date = existing["acquisition_date"]

            d1 = datetime.fromisoformat(
                date
            )

            d2 = datetime.fromisoformat(
                existing_date
            )

            difference = abs(
                (d1 - d2).days
            )

            if difference < 2:
                too_close = True
                break

        if not too_close:
            selected.append(product)

        if len(selected) >= MAX_EVENT_SCENES:
            break

    selected.sort(
        key=lambda x: x["datetime"]
    )

    return selected


# ============================================================
# SELECT BASELINE SCENES
# ============================================================

def select_baseline_scenes(products, preferred_orbits):

    products = remove_duplicate_acquisitions(products)

    # First preference:
    # same relative orbit as event imagery.
    same_orbit = [
        p for p in products
        if p["relative_orbit"] in preferred_orbits
    ]

    # If same relative orbit isn't available,
    # use all valid baseline scenes.
    if same_orbit:
        candidates = same_orbit
    else:
        candidates = products

    candidates = [
        p for p in candidates
        if p["bbox_overlap_score"] > 0
    ]

    candidates.sort(
        key=lambda x: (
            x["bbox_overlap_score"],
            x["datetime"]
        ),
        reverse=True
    )

    selected = []

    for product in candidates:

        date = product["acquisition_date"]

        too_close = False

        for existing in selected:

            existing_date = existing["acquisition_date"]

            d1 = datetime.fromisoformat(
                date
            )

            d2 = datetime.fromisoformat(
                existing_date
            )

            if abs((d1 - d2).days) < 2:
                too_close = True
                break

        if not too_close:
            selected.append(product)

        if len(selected) >= MAX_BASELINE_SCENES:
            break

    selected.sort(
        key=lambda x: x["datetime"]
    )

    return selected


# ============================================================
# MAIN
# ============================================================

print("\n" + "=" * 80)
print("SENTINEL-1 DATA COLLECTION")
print("=" * 80)

print(f"Event       : {EVENT_NAME}")
print(f"Study BBOX  : {BBOX}")

# ------------------------------------------------------------
# 1. Search baseline imagery
# ------------------------------------------------------------

print("\nSearching PRE-EVENT imagery...")
print(
    f"{BASELINE_START} -> {BASELINE_END}"
)

baseline_raw = search_sentinel(
    BASELINE_START,
    BASELINE_END
)

baseline_products = [
    extract_product(item)
    for item in baseline_raw
]

print(
    f"Raw baseline products found: "
    f"{len(baseline_products)}"
)

# ------------------------------------------------------------
# 2. Search event imagery
# ------------------------------------------------------------

print("\nSearching EVENT imagery...")
print(
    f"{EVENT_START} -> {EVENT_END}"
)

event_raw = search_sentinel(
    EVENT_START,
    EVENT_END
)

event_products = [
    extract_product(item)
    for item in event_raw
]

print(
    f"Raw event products found: "
    f"{len(event_products)}"
)

# ------------------------------------------------------------
# 3. Select event scenes
# ------------------------------------------------------------

selected_event = select_event_scenes(
    event_products
)

# Relative orbits represented by selected event scenes
preferred_orbits = {
    p["relative_orbit"]
    for p in selected_event
    if p["relative_orbit"] is not None
}

# ------------------------------------------------------------
# 4. Select baseline scenes
# ------------------------------------------------------------

selected_baseline = select_baseline_scenes(
    baseline_products,
    preferred_orbits
)

# ------------------------------------------------------------
# 5. Print selected scenes
# ------------------------------------------------------------

print("\n" + "=" * 80)
print("SELECTED BASELINE SCENES")
print("=" * 80)

for product in selected_baseline:

    print("\nProduct ID    :", product["product_id"])
    print("Date          :", product["datetime"])
    print("Orbit         :", product["orbit"])
    print(
        "Relative Orbit:",
        product["relative_orbit"]
    )
    print(
        "Coverage Score:",
        product["bbox_overlap_score"]
    )
    print(
        "Polarization  :",
        product["polarization"]
    )


print("\n" + "=" * 80)
print("SELECTED EVENT SCENES")
print("=" * 80)

for product in selected_event:

    print("\nProduct ID    :", product["product_id"])
    print("Date          :", product["datetime"])
    print("Orbit         :", product["orbit"])
    print(
        "Relative Orbit:",
        product["relative_orbit"]
    )
    print(
        "Coverage Score:",
        product["bbox_overlap_score"]
    )
    print(
        "Polarization  :",
        product["polarization"]
    )


# ============================================================
# SAVE COMPLETE SEARCH RESULTS
# ============================================================

all_results = {
    "source": "Copernicus Data Space Ecosystem",
    "collection": "sentinel-1-grd",
    "event": EVENT_NAME,
    "study_area_bbox": BBOX,

    "event_period": {
        "start": EVENT_START,
        "end": EVENT_END
    },

    "baseline_period": {
        "start": BASELINE_START,
        "end": BASELINE_END
    },

    "filters": {
        "product_type": "IW_GRDH_1S",
        "polarization": ["VV", "VH"]
    },

    "baseline_raw_count": len(baseline_products),
    "event_raw_count": len(event_products),

    "baseline_products": baseline_products,
    "event_products": event_products
}

with open(
    OUTPUT_DIR / "search_results.json",
    "w",
    encoding="utf-8"
) as file:

    json.dump(
        all_results,
        file,
        indent=2
    )


# ============================================================
# SAVE SELECTED PRODUCTS
# ============================================================

selected_results = {
    "source": "Copernicus Data Space Ecosystem",
    "event": EVENT_NAME,

    "selection_method": {
        "remove_same_day_orbit_duplicate_slices": True,
        "prefer_same_relative_orbit": True,
        "prefer_high_study_area_coverage": True,
        "minimum_days_between_selected_scenes": 2
    },

    "baseline_scenes": selected_baseline,
    "event_scenes": selected_event
}

with open(
    OUTPUT_DIR / "selected_products.json",
    "w",
    encoding="utf-8"
) as file:

    json.dump(
        selected_results,
        file,
        indent=2
    )


# ============================================================
# FINAL SUMMARY
# ============================================================

print("\n" + "=" * 80)
print("COLLECTION COMPLETE")
print("=" * 80)

print(
    f"Raw baseline products : "
    f"{len(baseline_products)}"
)

print(
    f"Raw event products    : "
    f"{len(event_products)}"
)

print(
    f"Selected baseline     : "
    f"{len(selected_baseline)}"
)

print(
    f"Selected event        : "
    f"{len(selected_event)}"
)

print("\nFiles created:")

print(
    OUTPUT_DIR / "search_results.json"
)

print(
    OUTPUT_DIR / "selected_products.json"
)

print("=" * 80)