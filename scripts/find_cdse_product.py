import requests

url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

params = {
    "$filter": (
        "Collection/Name eq 'SENTINEL-1' "
        "and startswith(Name,'S1D_IW_GRDH_1SDV_20261006') "
        "and ContentDate/Start ge 2026-10-06T00:10:00Z "
        "and ContentDate/Start lt 2026-10-06T00:13:00Z"
    ),
    "$top": 20,
}

response = requests.get(url, params=params, timeout=60)

print("STATUS:", response.status_code)

data = response.json()

for product in data.get("value", []):
    print("\nNAME:", product.get("Name"))
    print("ID:", product.get("Id"))
    print("START:", product.get("ContentDate", {}).get("Start"))
    print("SIZE:", product.get("ContentLength"))