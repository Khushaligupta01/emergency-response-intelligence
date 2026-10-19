import json
from pathlib import Path

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import shape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import (
    Disaster,
    DisasterSource,
    HazardObservation,
    SatelliteCatalogItem,
    SourceHttpState,
    SourceIngestionRun,
)
from app.services.source_adapters import (
    FetchResult,
    parse_gdacs_feature,
    parse_sachet_cap,
    parse_stac_item,
    parse_usgs_feature,
)
from app.services.source_ingestion import get_validator_headers, record_fetch_result
from app.services.source_adapters import FetchResult


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "source_ingestion"


@pytest.fixture()
def db_session():
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection)
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.mark.parametrize(
    "source,filename",
    [
        ("GDACS", "gdacs_feature.json"),
        ("USGS", "usgs_feature.json"),
        ("SACHET", "sachet_cap.xml.json"),
    ],
)
def test_official_event_ingestion_is_idempotent(db_session: Session, source, filename):
    fixture = json.loads((FIXTURE_DIR / filename).read_text(encoding="utf-8"))
    if source == "GDACS":
        event = parse_gdacs_feature(fixture["payload"])
    elif source == "USGS":
        event = parse_usgs_feature(fixture["payload"])
    else:
        event = parse_sachet_cap(
            fixture["payload"]["cap_xml"],
            source_url=fixture["fixture_source_url"],
            feed_guid=fixture["payload"]["rss_item"]["guid"],
        )
    source_count_before = db_session.scalar(
        select(func.count()).select_from(DisasterSource).where(
            DisasterSource.source_name == event.source_name,
            DisasterSource.external_id == event.external_id,
        )
    )
    observation_count_before = db_session.scalar(
        select(func.count())
        .select_from(HazardObservation)
        .join(DisasterSource, HazardObservation.source_id == DisasterSource.id)
        .where(
            DisasterSource.source_name == event.source_name,
            DisasterSource.external_id == event.external_id,
            HazardObservation.external_id == event.observation_id,
        )
    )
    run_count_before = db_session.scalar(
        select(func.count()).select_from(SourceIngestionRun).where(
            SourceIngestionRun.request_url == fixture["fixture_source_url"]
        )
    )
    result = FetchResult(
        source_name=event.source_name,
        request_url=fixture["fixture_source_url"],
        events=[event],
        items_seen=1,
    )

    first = record_fetch_result(db_session, result)
    second = record_fetch_result(db_session, result)
    db_session.flush()

    assert first.status == second.status == "success"
    assert first.items_persisted == second.items_persisted == 1
    assert db_session.scalar(
        select(func.count())
        .select_from(HazardObservation)
        .join(DisasterSource, HazardObservation.source_id == DisasterSource.id)
        .where(
            DisasterSource.source_name == event.source_name,
            DisasterSource.external_id == event.external_id,
            HazardObservation.external_id == event.observation_id,
        )
    ) == max(1, observation_count_before)
    assert db_session.scalar(
        select(func.count()).select_from(DisasterSource).where(
            DisasterSource.source_name == event.source_name,
            DisasterSource.external_id == event.external_id,
        )
    ) == max(1, source_count_before)
    assert db_session.scalar(
        select(func.count()).select_from(Disaster).where(
            Disaster.source == event.source_name,
            Disaster.external_id == event.external_id,
        )
    ) == 1
    assert db_session.scalar(
        select(func.count()).select_from(SourceIngestionRun).where(
            SourceIngestionRun.request_url == fixture["fixture_source_url"]
        )
    ) == run_count_before + 2


def test_official_copernicus_catalogue_item_is_upserted(db_session: Session):
    fixture = json.loads(
        (FIXTURE_DIR / "copernicus_sentinel-1-grd.json").read_text(encoding="utf-8")
    )
    item = parse_stac_item(fixture["payload"], collection_id="sentinel-1-grd")
    disaster_id = db_session.scalar(
        select(Disaster.id).where(
            Disaster.source == "GDACS",
            Disaster.external_id == "1104121",
        )
    )
    assert disaster_id is not None, "Step 4 Bihar event is required for catalogue association."
    count_before = db_session.scalar(
        select(func.count())
        .select_from(SatelliteCatalogItem)
        .join(DisasterSource, SatelliteCatalogItem.source_id == DisasterSource.id)
        .where(
            DisasterSource.source_name == "COPERNICUS",
            SatelliteCatalogItem.external_id == item["external_id"],
        )
    )
    result = FetchResult(
        source_name="COPERNICUS",
        request_url=fixture["fixture_source_url"],
        satellite_items=[item],
        items_seen=1,
    )

    first = record_fetch_result(db_session, result, disaster_id=disaster_id)
    second = record_fetch_result(db_session, result, disaster_id=disaster_id)
    db_session.flush()

    assert first.status == second.status == "success"
    assert first.items_persisted == second.items_persisted == 1
    assert db_session.scalar(
        select(func.count())
        .select_from(SatelliteCatalogItem)
        .join(DisasterSource, SatelliteCatalogItem.source_id == DisasterSource.id)
        .where(
            DisasterSource.source_name == "COPERNICUS",
            SatelliteCatalogItem.external_id == item["external_id"],
        )
    ) == max(1, count_before)


def test_duplicate_get_response_states_upsert_one_validator(db_session: Session):
    request_url = "https://test.invalid/source-validator"
    result = FetchResult(
        source_name="TEST_FIXTURE",
        request_url=request_url,
        response_metadata={
            "responses": [
                {
                    "url": request_url,
                    "method": "GET",
                    "status_code": 200,
                    "etag": '"fixture-validator"',
                    "last_modified": None,
                    "cache_control": "no-store",
                },
                {
                    "url": request_url,
                    "method": "GET",
                    "status_code": 200,
                    "etag": '"fixture-validator"',
                    "last_modified": None,
                    "cache_control": "no-store",
                },
            ]
        },
    )

    record_fetch_result(db_session, result)

    states = db_session.scalars(
        select(SourceHttpState).where(
            SourceHttpState.source_name == "TEST_FIXTURE",
            SourceHttpState.request_url == request_url,
        )
    ).all()
    assert len(states) == 1
    assert get_validator_headers(db_session, "TEST_FIXTURE", request_url) == {
        "If-None-Match": '"fixture-validator"'
    }


def test_event_listing_point_does_not_replace_existing_polygon(db_session: Session):
    fixture = json.loads(
        (FIXTURE_DIR / "gdacs_feature.json").read_text(encoding="utf-8")
    )
    event = parse_gdacs_feature(fixture["payload"])
    result = FetchResult(
        source_name=event.source_name,
        request_url=fixture["fixture_source_url"],
        events=[event],
        items_seen=1,
    )
    record_fetch_result(db_session, result)
    disaster = db_session.scalar(
        select(Disaster).where(
            Disaster.source == event.source_name,
            Disaster.external_id == event.external_id,
        )
    )
    assert disaster is not None

    # A transient test polygon represents a previously stored detailed area.
    retained_polygon = shape(event.geometry).buffer(0.1)
    disaster.geometry = from_shape(retained_polygon, srid=4326)
    db_session.flush()
    record_fetch_result(db_session, result)

    assert to_shape(disaster.geometry).equals_exact(retained_polygon, tolerance=1e-9)
