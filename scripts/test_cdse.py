import requests

url = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"

params = {
    "$filter": "startswith(Name,'S1D_IW_GRDH_1SDV_20261006')",
    "$top": 5,
}

response = requests.get(
    url,
    params=params,
    timeout=60,
)

print("STATUS:", response.status_code)
print(response.text[:5000])