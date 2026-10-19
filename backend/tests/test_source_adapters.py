import json
from pathlib import Path

import httpx
import pytest
from shapely.geometry import shape

from app.services.source_adapters import (
    SourceIntegrationError,
    SourcePayloadError,
    fetch_usgs,
    parse_datetime,
    parse_gdacs_feature,
    parse_sachet_cap,
    parse_sachet_rss,
    parse_stac_item,
    parse_usgs_feature,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "source_ingestion"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def test_parse_official_gdacs_feature_fixture():
    fixture = _fixture("gdacs_feature.json")
    event = parse_gdacs_feature(fixture["payload"])

    assert event.source_name == "GDACS"
    assert event.external_id == str(fixture["payload"]["properties"]["eventid"])
    assert event.event_type == fixture["payload"]["properties"]["eventtype"]
    assert event.metadata["episode_id"] == fixture["payload"]["properties"]["episodeid"]
    assert shape(event.geometry).is_valid
    assert event.occurred_at is not None and event.occurred_at.utcoffset().total_seconds() == 0


def test_parse_official_usgs_feature_fixture_preserves_depth_and_timestamp():
    fixture = _fixture("usgs_feature.json")
    event = parse_usgs_feature(fixture["payload"])
    assert event.external_id == fixture["payload"]["id"]
    assert event.event_type == "earthquake"
    assert event.metadata["depth_km"] == fixture["payload"]["geometry"]["coordinates"][2]
    assert list(event.geometry["coordinates"]) == fixture["payload"]["geometry"]["coordinates"][:2]
    assert event.occurred_at is not None


def test_parse_official_sachet_rss_and_cap_fixtures():
    rss_fixture = _fixture("sachet_rss.json")
    rss_items = parse_sachet_rss(rss_fixture["payload"])
    assert rss_items
    assert all(item["link"].startswith("https://sachet.ndma.gov.in/") for item in rss_items)

    fixture = _fixture("sachet_cap.xml.json")
    event = parse_sachet_cap(
        fixture["payload"]["cap_xml"],
        source_url=fixture["fixture_source_url"],
        feed_guid=fixture["payload"]["rss_item"]["guid"],
    )
    assert event.external_id
    assert event.event_type == "Flood"
    assert event.severity == "Moderate"
    assert event.geometry is None
    assert event.metadata["areas"][0]["areadesc"]


def test_cap_circle_geometry_uses_the_reported_radius():
    payload = """<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
      <identifier>test-circle</identifier>
      <sent>2026-10-18T12:00:00Z</sent>
      <info>
        <language>en</language><event>Test alert</event>
        <effective>2026-10-18T12:00:00Z</effective>
        <area><areaDesc>test fixture</areaDesc><circle>25.0,85.0 10</circle></area>
      </info>
    </alert>"""
    event = parse_sachet_cap(
        payload,
        source_url="https://sachet.ndma.gov.in/cap_public_website/FetchXMLFile?identifier=test",
    )
    geometry = shape(event.geometry)
    assert geometry.is_valid
    assert geometry.contains(shape({"type": "Point", "coordinates": [85.0, 25.0]}))
    assert 0.02 < geometry.area < 0.04


@pytest.mark.parametrize(
    "filename,collection",
    [
        ("copernicus_sentinel-1-grd.json", "sentinel-1-grd"),
        ("copernicus_sentinel-2-l2a.json", "sentinel-2-l2a"),
    ],
)
def test_parse_official_copernicus_item_fixtures(filename, collection):
    fixture = _fixture(filename)
    item = parse_stac_item(fixture["payload"], collection_id=collection)

    assert item["external_id"] == fixture["payload"]["id"]
    assert item["collection_id"] == collection
    assert item["assets"]
    assert shape(item["geometry"]).is_valid
    assert item["acquired_at"].utcoffset().total_seconds() == 0


def test_timestamp_parser_handles_iso_and_epoch_milliseconds():
    assert parse_datetime("2026-10-18T18:21:12+05:30").isoformat() == (
        "2026-10-18T12:51:12+00:00"
    )
    assert parse_datetime(1792345594400).year == 2026


def test_malformed_partial_source_records_fail_validation():
    gdacs_feature = _fixture("gdacs_feature.json")["payload"]
    malformed = {**gdacs_feature, "properties": {"eventid": gdacs_feature["properties"]["eventid"]}}
    with pytest.raises(SourcePayloadError, match="event type"):
        parse_gdacs_feature(malformed)

    sachet_fixture = _fixture("sachet_cap.xml.json")
    with pytest.raises(SourcePayloadError, match="malformed"):
        parse_sachet_cap(
            "<cap:alert",
            source_url=sachet_fixture["fixture_source_url"],
        )


def test_usgs_fetch_surfaces_http_failure_and_handles_not_modified():
    def failing_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    with httpx.Client(transport=httpx.MockTransport(failing_request)) as client:
        with pytest.raises(SourceIntegrationError, match="HTTP 503"):
            fetch_usgs(client)

    def unchanged_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(304, request=request)

    with httpx.Client(transport=httpx.MockTransport(unchanged_request)) as client:
        result = fetch_usgs(client, validator_headers={"If-None-Match": '"etag"'})
    assert result.not_modified
    assert not result.events
