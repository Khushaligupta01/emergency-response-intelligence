# Match each selected STAC scene to its native CDSE SAFE product,
# then download the matching products.

import json
import os
import getpass
import requests
from pathlib import Path
from datetime import datetime, timezone

STAC_FILE = Path(
    "data/raw/sentinel/sentinel1/bihar_flood_1104121/selected_products.json"
)

OUT_DIR = Path(
    "data/raw/sentinel/sentinel1/bihar_flood_1104121/products"
)
OUT_DIR.mkdir(parents=True, exist_ok=True)

CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products"

with open(STAC_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

scenes = data["baseline_scenes"][:2] + data["event_scenes"][:2]

username = input("CDSE username/email: ")
password = getpass.getpass("CDSE password: ")

token_response = requests.post(
    TOKEN_URL,
    data={
        "client_id": "cdse-public",
        "username": username,
        "password": password,
        "grant_type": "password",
    },
    timeout=60,
)

token_response.raise_for_status()
token = token_response.json()["access_token"]

headers = {"Authorization": f"Bearer {token}"}

print("\n=== MATCHING PRODUCTS ===")

matched = []

for scene in scenes:
    dt = datetime.fromisoformat(scene["datetime"].replace("Z", "+00:00"))
    date = dt.strftime("%Y%m%d")
    prefix = f"S1D_IW_GRDH_1SDV_{date}T{dt.strftime('%H%M')}"

    params = {
        "$filter": (
            "Collection/Name eq 'SENTINEL-1' "
            f"and startswith(Name,'{prefix}')"
        ),
        "$top": 50,
    }

    r = requests.get(CATALOGUE, params=params, timeout=60)
    r.raise_for_status()

    products = r.json().get("value", [])

    # Only native SAFE products, not COG representations
    products = [
        p for p in products
        if p["Name"].endswith(".SAFE")
        and "_COG.SAFE" not in p["Name"]
    ]

    if not products:
        print(f"NOT FOUND: {scene['datetime']}")
        continue

    # Find the product whose acquisition time is closest
    def time_diff(p):
        ptime = datetime.fromisoformat(
            p["ContentDate"]["Start"].replace("Z", "+00:00")
        )
        return abs((ptime - dt).total_seconds())

    product = min(products, key=time_diff)

    print("\nFOUND:")
    print(product["Name"])
    print("ID:", product["Id"])
    print("Size:", product["ContentLength"])

    matched.append({
        "stac_datetime": scene["datetime"],
        "stac_product_id": scene["product_id"],
        "cdse_id": product["Id"],
        "cdse_name": product["Name"],
        "content_length": product["ContentLength"],
    })

with open(OUT_DIR.parent / "cdse_product_mapping.json", "w") as f:
    json.dump(matched, f, indent=2)

print(f"\nMatched {len(matched)} / {len(scenes)} scenes.")

if len(matched) != len(scenes):
    print("Some scenes could not be matched. No downloads started.")
    raise SystemExit

answer = input("\nDownload all matched products? (yes/no): ")

if answer.lower() != "yes":
    print("Download cancelled.")
    raise SystemExit

print("\n=== DOWNLOADING ===")

for i, product in enumerate(matched, 1):
    filename = product["cdse_name"]
    output = OUT_DIR / filename

    print(f"\n[{i}/{len(matched)}] {filename}")

    url = f"{DOWNLOAD_URL}({product['cdse_id']})/$value"

    with requests.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()

        with open(output, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

    print("Downloaded:", output)

print("\n=== SENTINEL-1 COLLECTION COMPLETE ===")