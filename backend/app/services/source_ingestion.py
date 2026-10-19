from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from geoalchemy2.shape import from_shape
from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    Disaster,
    DisasterSource,
    HazardObservation,
    SatelliteCatalogItem,
    SourceHttpState,
    SourceIngestionRun,
)
from app.services.source_adapters import FetchResult, NormalizedEvent


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return {"previous_metadata": value}
    return result if isinstance(result, dict) else {"previous_metadata": result}


def _geoalchemy_geometry(geometry: dict[str, Any] | None):
    if geometry is None:
        return None
    return from_shape(shape(geometry), srid=4326)


def get_validator_headers(
    session: Session,
    source_name: str,
    request_url: str,
) -> dict[str, str]:
    state = session.scalar(
        select(SourceHttpState).where(
            SourceHttpState.source_name == source_name,
            SourceHttpState.request_url == request_url,
        )
    )
    if state is None:
        return {}
    headers = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified
    return headers


def _persist_event(session: Session, event: NormalizedEvent) -> None:
    if len(event.external_id) > 100:
        raise ValueError(f"{event.source_name} external event ID exceeds 100 characters.")
    if len(event.observation_id) > 200:
        raise ValueError(f"{event.source_name} observation ID exceeds 200 characters.")
    if len(event.source_name) > 50:
        raise ValueError("Source name exceeds the database field limit.")
    if event.source_url and len(event.source_url) > 1000:
        raise ValueError("Source URL exceeds the database field limit.")
    disaster = session.scalar(
        select(Disaster).where(
            Disaster.source == event.source_name,
            Disaster.external_id == event.external_id,
        )
    )
    event_metadata = dict(event.metadata)
    event_metadata["source_url"] = event.source_url
    event_metadata["source_event_name"] = event.name
    if event.country is not None:
        event_metadata["source_country"] = event.country
    if event.country is None:
        event_metadata["country_not_provided_by_source"] = True

    if disaster is None:
        disaster = Disaster(
            source=event.source_name,
            external_id=event.external_id,
            disaster_type=event.event_type[:50],
            name=event.name[:255],
            country=(event.country or "Unknown (not provided by source)")[:100],
            severity=event.severity[:30] if event.severity else None,
            start_time=event.occurred_at,
            end_time=event.expires_at,
            geometry=_geoalchemy_geometry(event.geometry),
            metadata_json=_json_text(event_metadata),
        )
        session.add(disaster)
        session.flush()
    else:
        disaster.disaster_type = event.event_type[:50]
        disaster.name = event.name[:255]
        disaster.severity = event.severity[:30] if event.severity else disaster.severity
        disaster.start_time = event.occurred_at or disaster.start_time
        disaster.end_time = event.expires_at or disaster.end_time
        if event.country:
            disaster.country = event.country[:100]
        # Existing event geometries may be a more detailed, analyst-reported area.
        if disaster.geometry is None and event.geometry is not None:
            disaster.geometry = _geoalchemy_geometry(event.geometry)
        old_metadata = _json_object(disaster.metadata_json)
        old_metadata["latest_ingestion"] = event_metadata
        disaster.metadata_json = _json_text(old_metadata)
        session.flush()

    payload_hash = hashlib.sha256(_json_text(event_metadata).encode("utf-8")).hexdigest()
    source_record = session.scalar(
        select(DisasterSource).where(
            DisasterSource.source_name == event.source_name,
            DisasterSource.external_id == event.external_id,
        )
    )
    if source_record is None:
        source_record = DisasterSource(
            disaster_id=disaster.id,
            source_name=event.source_name,
            external_id=event.external_id,
            source_url=event.source_url,
            fetched_at=datetime.now(timezone.utc),
            payload_hash=payload_hash,
            metadata_json=_json_text(event_metadata),
        )
        session.add(source_record)
        session.flush()
    else:
        if source_record.disaster_id != disaster.id:
            raise ValueError(
                f"{event.source_name} external ID {event.external_id!r} is already "
                "associated with a different disaster."
            )
        source_metadata = _json_object(source_record.metadata_json)
        source_metadata["latest_ingestion"] = event_metadata
        source_record.source_url = event.source_url or source_record.source_url
        source_record.fetched_at = datetime.now(timezone.utc)
        source_record.payload_hash = payload_hash
        source_record.metadata_json = _json_text(source_metadata)
        session.flush()

    observation = session.scalar(
        select(HazardObservation).where(
            HazardObservation.source_id == source_record.id,
            HazardObservation.external_id == event.observation_id,
        )
    )
    if observation is None:
        observation = HazardObservation(
            disaster_id=disaster.id,
            source_id=source_record.id,
            hazard_type=event.event_type[:50],
            external_id=event.observation_id[:200],
            observed_at=event.observed_at,
            geometry=_geoalchemy_geometry(event.geometry),
            metadata_json=_json_text(event_metadata),
        )
        session.add(observation)
    else:
        observation.observed_at = event.observed_at or observation.observed_at
        if observation.geometry is None and event.geometry is not None:
            observation.geometry = _geoalchemy_geometry(event.geometry)
        old_metadata = _json_object(observation.metadata_json)
        old_metadata["latest_ingestion"] = event_metadata
        observation.metadata_json = _json_text(old_metadata)


def _persist_satellite_item(
    session: Session,
    item: dict[str, Any],
    *,
    disaster_id: int | None,
) -> None:
    external_id = str(item["external_id"])
    source_external_id = f"STAC:{external_id}"
    if len(source_external_id) > 150:
        raise ValueError("Copernicus item ID exceeds the source-provenance field limit.")
    source_record = session.scalar(
        select(DisasterSource).where(
            DisasterSource.source_name == "COPERNICUS",
            DisasterSource.external_id == source_external_id,
        )
    )
    item_metadata = dict(item["metadata"])
    item_metadata["collection_id"] = item["collection_id"]
    if source_record is None:
        if disaster_id is None:
            raise ValueError("Copernicus catalogue items require an associated disaster.")
        source_record = DisasterSource(
            disaster_id=disaster_id,
            source_name="COPERNICUS",
            external_id=source_external_id,
            source_url=item["item_url"],
            fetched_at=datetime.now(timezone.utc),
            payload_hash=hashlib.sha256(_json_text(item_metadata).encode("utf-8")).hexdigest(),
            metadata_json=_json_text(item_metadata),
        )
        session.add(source_record)
        session.flush()
    elif disaster_id is not None and source_record.disaster_id != disaster_id:
        raise ValueError(
            f"Copernicus item {external_id!r} is already associated with another disaster."
        )
    else:
        source_record.source_url = item["item_url"] or source_record.source_url
        source_record.fetched_at = datetime.now(timezone.utc)
        source_record.payload_hash = hashlib.sha256(
            _json_text(item_metadata).encode("utf-8")
        ).hexdigest()
        source_record.metadata_json = _json_text(item_metadata)

    catalog_item = session.scalar(
        select(SatelliteCatalogItem).where(
            SatelliteCatalogItem.source_id == source_record.id,
            SatelliteCatalogItem.external_id == external_id,
        )
    )
    values = {
        "disaster_id": disaster_id,
        "collection_id": item["collection_id"],
        "acquired_at": item["acquired_at"],
        "geometry": _geoalchemy_geometry(item["geometry"]),
        "bbox": item["bbox"],
        "cloud_cover": item["cloud_cover"],
        "item_url": item["item_url"],
        "assets": item["assets"],
        "metadata_json": item_metadata,
    }
    if catalog_item is None:
        session.add(
            SatelliteCatalogItem(
                source_id=source_record.id,
                external_id=external_id,
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(catalog_item, key, value)


def _upsert_http_state(
    session: Session,
    source_name: str,
    response: dict[str, Any],
) -> None:
    if response.get("method", "GET").upper() != "GET":
        return
    request_url = response.get("url")
    if not isinstance(request_url, str) or not request_url.startswith(("https://", "http://")):
        return
    response_status = response.get("status_code")
    if not isinstance(response_status, int):
        return
    state = session.scalar(
        select(SourceHttpState).where(
            SourceHttpState.source_name == source_name,
            SourceHttpState.request_url == request_url,
        )
    )
    if state is None:
        state = SourceHttpState(source_name=source_name, request_url=request_url)
        session.add(state)
        session.flush()
    state.response_status = response_status
    state.validated_at = datetime.now(timezone.utc)
    state.cache_control = response.get("cache_control")
    state.etag = response.get("etag") or state.etag
    state.last_modified = response.get("last_modified") or state.last_modified


def record_fetch_result(
    session: Session,
    result: FetchResult,
    *,
    disaster_id: int | None = None,
) -> SourceIngestionRun:
    persisted = 0
    failures = list(result.errors)
    events = [(event, None) for event in result.events]
    satellite_items = [(None, item) for item in result.satellite_items]
    for event, item in events + satellite_items:
        try:
            with session.begin_nested():
                if event is not None:
                    _persist_event(session, event)
                else:
                    _persist_satellite_item(session, item, disaster_id=disaster_id)
            persisted += 1
        except (SQLAlchemyError, TypeError, ValueError, KeyError) as error:
            failures.append(f"Persistence error: {error}")

    if not failures:
        for response in result.response_metadata.get("responses", []):
            if isinstance(response, dict):
                _upsert_http_state(session, result.source_name, response)
    run = SourceIngestionRun(
        source_name=result.source_name,
        request_url=result.request_url,
        status=(
            "not_modified"
            if result.not_modified
            else "partial"
            if failures
            else "success"
        ),
        completed_at=datetime.now(timezone.utc),
        items_seen=result.items_seen,
        items_persisted=persisted,
        items_failed=len(failures),
        error_message="\n".join(failures)[:10000] or None,
        response_metadata=result.response_metadata,
    )
    session.add(run)
    return run


def record_fetch_failure(
    session: Session,
    *,
    source_name: str,
    request_url: str,
    error: Exception,
) -> SourceIngestionRun:
    run = SourceIngestionRun(
        source_name=source_name,
        request_url=request_url,
        status="failed",
        completed_at=datetime.now(timezone.utc),
        items_failed=1,
        error_message=str(error)[:10000],
        response_metadata={},
    )
    session.add(run)
    return run
