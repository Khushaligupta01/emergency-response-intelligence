from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_overview_returns_persisted_gdacs_event_and_exposure():
    response = client.get("/api/v1/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["disaster"]["source"] == "GDACS"
    assert body["disaster"]["external_id"] == "1104121"
    assert body["affected_area"]["episode_id"] == "20"
    assert "geometry" not in body["affected_area"]
    assert len(response.content) < 100_000
    assert body["zone_count"] > 0
    assert body["buildings"]["affected_buildings"] > 0


def test_zone_and_affected_area_endpoints_return_geojson():
    zones = client.get("/api/v1/zones")
    area = client.get("/api/v1/map/affected-area")

    assert zones.status_code == 200
    assert zones.json()["type"] == "FeatureCollection"
    assert zones.json()["features"]
    assert area.status_code == 200
    assert area.json()["type"] == "FeatureCollection"
    assert len(area.json()["features"]) == 1
    assert area.json()["features"][0]["geometry"] is not None


def test_building_endpoint_keeps_low_zoom_summary_and_caps_spatial_results():
    summary = client.get(
        "/api/v1/buildings",
        params={"west": 84.13, "south": 27.43, "east": 84.25, "north": 27.51, "zoom": 8},
    )
    detail = client.get(
        "/api/v1/buildings",
        params={"west": 84.13, "south": 27.43, "east": 84.25, "north": 27.51, "zoom": 13},
    )

    assert summary.status_code == 200
    assert summary.json()["features"] == []
    assert summary.json()["summary"]["count"] > 0
    assert detail.status_code == 200
    assert detail.json()["type"] == "FeatureCollection"
    assert 0 < len(detail.json()["features"]) <= 1000


def test_read_only_inventory_source_and_health_endpoints():
    inventory = client.get("/api/v1/resources")
    sources = client.get("/api/v1/sources")
    health = client.get("/api/v1/health")

    assert inventory.status_code == 200
    assert {"balances", "resource_types", "depots", "transactions"} <= inventory.json().keys()
    assert sources.status_code == 200
    assert {item["name"] for item in sources.json()["sources"]} == {
        "GDACS",
        "USGS",
        "NDMA / SACHET",
        "Copernicus Sentinel-1",
        "Copernicus Sentinel-2",
        "OpenStreetMap",
        "WorldPop",
    }
    assert health.status_code == 200
    assert {item["name"] for item in health.json()["checks"]} >= {"FastAPI", "PostgreSQL", "PostGIS"}
