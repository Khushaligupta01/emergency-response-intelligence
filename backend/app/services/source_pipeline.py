from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import Disaster, HazardObservation
from app.services.source_adapters import (
    COPERNICUS_STAC_SEARCH,
    GDACS_API,
    SACHET_BIHAR_FEED,
    USGS_ALL_DAY_FEED,
    FetchResult,
    SourceIntegrationError,
    SourcePayloadError,
    fetch_copernicus_stac,
    fetch_gdacs,
    fetch_sachet,
    fetch_usgs,
)
from app.services.source_ingestion import (
    get_validator_headers,
    record_fetch_failure,
    record_fetch_result,
)


BIHAR_GDACS_EXTERNAL_ID = "1104121"
BIHAR_AFFECTED_OBSERVATION_ID = "FL-1104121-EP20-Poly_Affected"


def _run_result(session: Session, result: FetchResult, *, disaster_id: int | None = None) -> dict:
    run = record_fetch_result(session, result, disaster_id=disaster_id)
    session.commit()
    return {
        "source": run.source_name,
        "status": run.status,
        "items_seen": run.items_seen,
        "items_persisted": run.items_persisted,
        "items_failed": run.items_failed,
        "error": run.error_message,
    }


def _run_failure(
    session: Session,
    source_name: str,
    request_url: str,
    error: Exception,
) -> dict:
    run = record_fetch_failure(
        session,
        source_name=source_name,
        request_url=request_url,
        error=error,
    )
    session.commit()
    return {
        "source": run.source_name,
        "status": run.status,
        "items_seen": 0,
        "items_persisted": 0,
        "items_failed": run.items_failed,
        "error": run.error_message,
    }


def _fetch_source(
    session: Session,
    source_name: str,
    request_url: str,
    fetch: Callable[[], FetchResult],
) -> dict:
    try:
        return _run_result(session, fetch())
    except SQLAlchemyError as error:
        session.rollback()
        return _run_failure(session, source_name, request_url, error)
    except (httpx.HTTPError, SourceIntegrationError, SourcePayloadError, ValueError) as error:
        return _run_failure(session, source_name, request_url, error)


def _bihar_event_area(session: Session) -> tuple[Disaster, dict, list[float]]:
    disaster = session.scalar(
        select(Disaster).where(
            Disaster.source == "GDACS",
            Disaster.external_id == BIHAR_GDACS_EXTERNAL_ID,
        )
    )
    if disaster is None:
        raise ValueError(
            f"GDACS disaster {BIHAR_GDACS_EXTERNAL_ID} is missing; "
            "apply the Step 4 data loader before Copernicus discovery."
        )
    observation = session.scalar(
        select(HazardObservation).where(
            HazardObservation.disaster_id == disaster.id,
            HazardObservation.external_id == BIHAR_AFFECTED_OBSERVATION_ID,
        )
    )
    if observation is None or observation.geometry is None:
        raise ValueError(
            "The stored Episode 20 Poly_Affected observation is missing; "
            "Copernicus discovery will not substitute another GDACS geometry."
        )
    if disaster.start_time is None:
        raise ValueError("The GDACS event has no start time for satellite discovery.")
    end = disaster.end_time or disaster.start_time
    if end < disaster.start_time:
        raise ValueError("The stored GDACS event date range is invalid.")
    geometry = to_shape(observation.geometry)
    return disaster, mapping(geometry), list(geometry.bounds)


def ingest_sources(
    session: Session,
    client: httpx.Client,
    *,
    from_date: datetime,
    to_date: datetime,
    include_copernicus: bool = True,
) -> list[dict]:
    if from_date.tzinfo is None or to_date.tzinfo is None:
        raise ValueError("Ingestion date range must include timezone information.")
    if to_date < from_date:
        raise ValueError("Ingestion end date must not precede start date.")

    results = []
    gdacs_url = str(
        httpx.URL(
            GDACS_API,
            params={
                "fromdate": from_date.astimezone(timezone.utc).date().isoformat(),
                "todate": to_date.astimezone(timezone.utc).date().isoformat(),
            },
        )
    )
    results.append(
        _fetch_source(
            session,
            "GDACS",
            gdacs_url,
            lambda: fetch_gdacs(
                client,
                from_date=from_date,
                to_date=to_date,
                validator_headers=get_validator_headers(session, "GDACS", gdacs_url),
            ),
        )
    )
    results.append(
        _fetch_source(
            session,
            "USGS",
            USGS_ALL_DAY_FEED,
            lambda: fetch_usgs(
                client,
                validator_headers=get_validator_headers(session, "USGS", USGS_ALL_DAY_FEED),
            ),
        )
    )
    results.append(
        _fetch_source(
            session,
            "SACHET",
            SACHET_BIHAR_FEED,
            lambda: fetch_sachet(
                client,
                validator_headers=get_validator_headers(
                    session,
                    "SACHET",
                    SACHET_BIHAR_FEED,
                ),
                validator_for_url=lambda url: get_validator_headers(session, "SACHET", url),
            ),
        )
    )

    if include_copernicus:
        try:
            disaster, geometry, bbox = _bihar_event_area(session)
            result = fetch_copernicus_stac(
                client,
                bbox=bbox,
                geometry=geometry,
                start=disaster.start_time,
                end=disaster.end_time or disaster.start_time,
            )
            results.append(_run_result(session, result, disaster_id=disaster.id))
        except (httpx.HTTPError, SourceIntegrationError, SourcePayloadError, ValueError) as error:
            results.append(_run_failure(session, "COPERNICUS", COPERNICUS_STAC_SEARCH, error))
    return results


def default_query_window(days: int = 7) -> tuple[datetime, datetime]:
    if days < 1:
        raise ValueError("The query window must be at least one day.")
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end
