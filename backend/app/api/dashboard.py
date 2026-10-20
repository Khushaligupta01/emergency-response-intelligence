from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["dashboard"])

BIHAR_EVENT_ID = "1104121"
BIHAR_AFFECTED_AREA_ID = "FL-1104121-EP20-Poly_Affected"


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _one(session: Session, statement: str, **params: Any) -> dict[str, Any] | None:
    row = session.execute(text(statement), params).mappings().first()
    return dict(row) if row else None


def _all(session: Session, statement: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(statement), params).mappings()]


def _resolve_disaster_id(session: Session, disaster_id: int | None) -> int:
    if disaster_id is not None:
        return disaster_id
    selected = _one(
        session,
        "SELECT id FROM disasters WHERE source = 'GDACS' AND external_id = :external_id",
        external_id=BIHAR_EVENT_ID,
    )
    if selected is None:
        raise HTTPException(status_code=404, detail="The configured GDACS event is not in the database.")
    return selected["id"]


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    disaster = _one(
        db,
        """
        SELECT id, external_id, source, disaster_type, name, country, severity,
               start_time, end_time, updated_at
        FROM disasters
        WHERE source = 'GDACS' AND external_id = :external_id
        """,
        external_id=BIHAR_EVENT_ID,
    )
    if disaster is None:
        raise HTTPException(status_code=404, detail="The configured GDACS event is not in the database.")

    disaster_id = disaster["id"]
    affected = _one(
        db,
        """
        SELECT h.external_id, h.hazard_type,
               substring(h.external_id from '-EP([0-9]+)-') AS episode_id,
               ST_Area(h.geometry::geography) / 1000000.0 AS area_km2
        FROM hazard_observations h
        WHERE h.disaster_id = :disaster_id AND h.external_id = :external_id
        """,
        disaster_id=disaster_id,
        external_id=BIHAR_AFFECTED_AREA_ID,
    )
    population = _one(
        db,
        """
        SELECT COALESCE(SUM(total_population), 0) AS total_population,
               COALESCE(SUM(affected_population), 0) AS affected_population,
               COALESCE(SUM(total_valid_pixels), 0) AS valid_population_pixels,
               COALESCE(SUM(affected_pixels), 0) AS affected_population_pixels
        FROM population_exposures WHERE disaster_id = :disaster_id
        """,
        disaster_id=disaster_id,
    )
    buildings = _one(
        db,
        """
        SELECT COUNT(*) AS affected_buildings,
               COALESCE(SUM(footprint_area_km2), 0) AS affected_footprint_area_km2
        FROM building_exposure_footprints WHERE disaster_id = :disaster_id
        """,
        disaster_id=disaster_id,
    )
    zone_counts = _all(
        db,
        """
        SELECT COALESCE(severity_class, 'Not classified') AS severity_class, COUNT(*) AS count
        FROM impact_zones WHERE disaster_id = :disaster_id
        GROUP BY severity_class ORDER BY severity_class
        """,
        disaster_id=disaster_id,
    )
    latest_update = _one(
        db,
        """
        SELECT MAX(GREATEST(d.updated_at, COALESCE(ds.fetched_at, d.updated_at))) AS last_data_update
        FROM disasters d
        LEFT JOIN disaster_sources ds ON ds.disaster_id = d.id
        WHERE d.id = :disaster_id
        """,
        disaster_id=disaster_id,
    )
    recent_runs = _all(
        db,
        """
        SELECT id, source_name, status, started_at, completed_at,
               items_seen, items_persisted, items_failed, error_message
        FROM source_ingestion_runs
        ORDER BY started_at DESC LIMIT 8
        """,
    )
    for row in recent_runs:
        for key, value in row.items():
            row[key] = _json_value(value)

    severity = {row["severity_class"]: row["count"] for row in zone_counts}
    return {
        "disaster": {key: _json_value(value) for key, value in disaster.items()},
        "affected_area": (
            {key: _json_value(value) for key, value in affected.items()}
            if affected
            else None
        ),
        "population": population,
        "buildings": buildings,
        "zone_count": sum(row["count"] for row in zone_counts),
        "severity_distribution": zone_counts,
        "critical_zones": severity.get("Critical", 0),
        "high_zones": severity.get("High", 0),
        "last_data_update": _json_value(
            latest_update["last_data_update"] if latest_update else None
        ),
        "recent_ingestion": recent_runs,
    }


@router.get("/zones")
def zones(
    disaster_id: int | None = Query(None, ge=1),
    west: float | None = None,
    south: float | None = None,
    east: float | None = None,
    north: float | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    disaster_id = _resolve_disaster_id(db, disaster_id)
    bbox_values = (west, south, east, north)
    if any(value is not None for value in bbox_values):
        if any(value is None for value in bbox_values):
            raise HTTPException(status_code=422, detail="Provide west, south, east, and north together.")
        if west >= east or south >= north or west < -180 or east > 180 or south < -90 or north > 90:
            raise HTTPException(status_code=422, detail="The requested map bounds are invalid.")
        predicate = "AND ST_Intersects(geometry, ST_MakeEnvelope(:west, :south, :east, :north, 4326))"
    else:
        predicate = ""
    features = _all(
        db,
        f"""
        SELECT id, zone_code, severity_class, impact_score, population_count,
               building_count, building_footprint_area_km2, road_length_km,
               hazard_coverage, population_exposure, building_exposure,
               infrastructure_exposure, vulnerability_score,
               ST_AsGeoJSON(geometry)::json AS geometry
        FROM impact_zones
        WHERE disaster_id = :disaster_id {predicate}
        ORDER BY zone_code
        """,
        disaster_id=disaster_id,
        west=west,
        south=south,
        east=east,
        north=north,
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": item.pop("id"),
                "geometry": item.pop("geometry"),
                "properties": item,
            }
            for item in features
        ],
    }


@router.get("/buildings")
def buildings(
    disaster_id: int | None = Query(None, ge=1),
    west: float | None = None,
    south: float | None = None,
    east: float | None = None,
    north: float | None = None,
    zoom: int = Query(8, ge=0, le=22),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    disaster_id = _resolve_disaster_id(db, disaster_id)
    summary = _one(
        db,
        """
        SELECT COUNT(*) AS count,
               COALESCE(SUM(footprint_area_km2), 0) AS area_km2
        FROM building_exposure_footprints WHERE disaster_id = :disaster_id
        """,
        disaster_id=disaster_id,
    )
    if zoom < 13 or any(value is None for value in (west, south, east, north)):
        return {"type": "FeatureCollection", "features": [], "summary": summary, "truncated": False}
    if west >= east or south >= north or west < -180 or east > 180 or south < -90 or north > 90:
        raise HTTPException(status_code=422, detail="The requested map bounds are invalid.")

    items = _all(
        db,
        """
        SELECT id, source_feature_id, footprint_area_km2,
               ST_AsGeoJSON(geometry)::json AS geometry
        FROM building_exposure_footprints
        WHERE disaster_id = :disaster_id
          AND ST_Intersects(geometry, ST_MakeEnvelope(:west, :south, :east, :north, 4326))
        ORDER BY id LIMIT 1001
        """,
        disaster_id=disaster_id,
        west=west,
        south=south,
        east=east,
        north=north,
    )
    truncated = len(items) > 1000
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": item["id"],
                "geometry": item["geometry"],
                "properties": {
                    "source_feature_id": item["source_feature_id"],
                    "footprint_area_km2": item["footprint_area_km2"],
                },
            }
            for item in items[:1000]
        ],
        "summary": summary,
        "truncated": truncated,
    }


@router.get("/resources")
def resources(db: Session = Depends(get_db)) -> dict[str, Any]:
    balances = _all(
        db,
        """
        SELECT rt.id AS resource_type_id, rt.code, rt.name AS resource_type,
               rt.category, rt.unit, d.id AS depot_id, d.name AS depot,
               ib.quantity, ib.reserved_quantity,
               ib.quantity - ib.reserved_quantity AS available_quantity,
               ib.updated_at
        FROM inventory_balances ib
        JOIN resource_types rt ON rt.id = ib.resource_type_id
        JOIN resource_depots d ON d.id = ib.depot_id
        ORDER BY rt.name, d.name
        """,
    )
    types = _all(
        db,
        "SELECT id, code, name, category, unit, is_active FROM resource_types ORDER BY name",
    )
    depots = _all(
        db,
        """
        SELECT id, name, district, state, is_active,
               ST_AsGeoJSON(geometry)::json AS geometry
        FROM resource_depots ORDER BY name
        """,
    )
    transactions = _all(
        db,
        """
        SELECT it.id, it.transaction_type, it.quantity, it.source, it.reference,
               it.notes, it.created_by, it.occurred_at,
               rt.name AS resource_type, rt.unit,
               d.name AS depot, destination.name AS destination_depot
        FROM inventory_transactions it
        JOIN resource_types rt ON rt.id = it.resource_type_id
        JOIN resource_depots d ON d.id = it.depot_id
        LEFT JOIN resource_depots destination ON destination.id = it.destination_depot_id
        ORDER BY it.occurred_at DESC LIMIT 100
        """,
    )
    for row in transactions:
        row["occurred_at"] = _json_value(row["occurred_at"])
    return {
        "balances": balances,
        "resource_types": types,
        "depots": [
            {
                "type": "Feature",
                "id": item["id"],
                "geometry": item.pop("geometry"),
                "properties": item,
            }
            for item in depots
        ],
        "transactions": transactions,
    }


@router.get("/sources")
def sources(db: Session = Depends(get_db)) -> dict[str, Any]:
    latest = _all(
        db,
        """
        SELECT DISTINCT ON (source_name) source_name, status, started_at, completed_at,
               items_seen, items_persisted, items_failed, error_message
        FROM source_ingestion_runs
        ORDER BY source_name, started_at DESC
        """,
    )
    records = _all(
        db,
        "SELECT source_name, COUNT(*) AS record_count FROM disaster_sources GROUP BY source_name",
    )
    satellite = _all(
        db,
        "SELECT collection_id, COUNT(*) AS record_count FROM satellite_catalog_items GROUP BY collection_id",
    )
    result: dict[str, Any] = {}
    for row in latest:
        name = row.pop("source_name")
        row["started_at"] = _json_value(row["started_at"])
        row["completed_at"] = _json_value(row["completed_at"])
        result[name] = row
    record_counts = {row["source_name"]: row["record_count"] for row in records}
    collection_counts = {row["collection_id"].lower(): row["record_count"] for row in satellite}
    return {
        "sources": [
            {
                "id": "GDACS",
                "name": "GDACS",
                "description": "Global Disaster Alert and Coordination System event feeds.",
                "record_count": record_counts.get("GDACS", 0),
                "latest": result.get("GDACS"),
            },
            {
                "id": "USGS",
                "name": "USGS",
                "description": "Official USGS earthquake event feeds.",
                "record_count": record_counts.get("USGS", 0),
                "latest": result.get("USGS"),
            },
            {
                "id": "SACHET",
                "name": "NDMA / SACHET",
                "description": "Official NDMA CAP alerts.",
                "record_count": record_counts.get("SACHET", 0),
                "latest": result.get("SACHET"),
            },
            {
                "id": "COPERNICUS",
                "name": "Copernicus Sentinel-1",
                "description": "Satellite imagery catalogue discovery only; damage analysis is not implemented.",
                "record_count": sum(v for k, v in collection_counts.items() if "sentinel-1" in k),
                "latest": result.get("COPERNICUS"),
            },
            {
                "id": "COPERNICUS",
                "name": "Copernicus Sentinel-2",
                "description": "Satellite imagery catalogue discovery only; damage analysis is not implemented.",
                "record_count": sum(v for k, v in collection_counts.items() if "sentinel-2" in k),
                "latest": result.get("COPERNICUS"),
            },
            {
                "id": "OSM",
                "name": "OpenStreetMap",
                "description": "Building footprints used for exposure assessment.",
                "record_count": record_counts.get("OSM", 0),
                "latest": None,
            },
            {
                "id": "WorldPop",
                "name": "WorldPop",
                "description": "Population dataset provenance and exposure summaries; source raster stays server-side.",
                "record_count": record_counts.get("WorldPop", 0),
                "latest": None,
            },
        ]
    }


@router.get("/health")
def system_health(db: Session = Depends(get_db)) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [{"name": "FastAPI", "status": "Connected", "detail": "API request received."}]
    try:
        database_name = db.execute(text("SELECT current_database()")).scalar_one()
    except SQLAlchemyError:
        db.rollback()
        checks.extend(
            [
                {"name": "PostgreSQL", "status": "Unavailable", "detail": "Database query failed."},
                {"name": "PostGIS", "status": "Unknown", "detail": "Database connection unavailable."},
            ]
        )
        checks.extend(
            {"name": name, "status": "Unknown", "detail": "Database unavailable."}
            for name in ("GDACS", "USGS", "NDMA/SACHET", "Copernicus")
        )
        return {"status": "degraded", "checks": checks}

    checks.append({"name": "PostgreSQL", "status": "Connected", "detail": database_name})
    try:
        postgis_version = db.execute(text("SELECT postgis_version()")).scalar_one()
        checks.append({"name": "PostGIS", "status": "Connected", "detail": postgis_version})
    except SQLAlchemyError:
        db.rollback()
        checks.append({"name": "PostGIS", "status": "Unavailable", "detail": "PostGIS extension query failed."})

    try:
        latest_runs = _all(
            db,
            """
            SELECT DISTINCT ON (source_name) source_name, status, started_at, error_message
            FROM source_ingestion_runs
            WHERE source_name IN ('GDACS', 'USGS', 'SACHET', 'COPERNICUS')
            ORDER BY source_name, started_at DESC
            """,
        )
        by_source = {run["source_name"]: run for run in latest_runs}
        for source_name, label in (
            ("GDACS", "GDACS"),
            ("USGS", "USGS"),
            ("SACHET", "NDMA/SACHET"),
            ("COPERNICUS", "Copernicus"),
        ):
            run = by_source.get(source_name)
            if run is None:
                state, detail = "Unknown", "No ingestion record is available."
            else:
                age_days = (datetime.now(timezone.utc) - run["started_at"]).total_seconds() / 86400
                state = (
                    "Connected"
                    if run["status"] in ("success", "not_modified") and age_days <= 14
                    else "Degraded"
                )
                detail = run["error_message"] or f"Latest recorded run: {run['status']}."
            checks.append({"name": label, "status": state, "detail": detail})
    except SQLAlchemyError:
        db.rollback()
        checks.extend(
            {
                "name": source_name,
                "status": "Unknown",
                "detail": "Could not query ingestion history.",
            }
            for source_name in ("GDACS", "USGS", "NDMA/SACHET", "Copernicus")
        )
    return {
        "status": "healthy"
        if all(check["status"] == "Connected" for check in checks)
        else "degraded",
        "checks": checks,
    }


@router.get("/map/affected-area")
def affected_area(
    disaster_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    disaster_id = _resolve_disaster_id(db, disaster_id)
    item = _one(
        db,
        """
        SELECT h.external_id, h.hazard_type, ST_AsGeoJSON(h.geometry)::json AS geometry
        FROM hazard_observations h
        WHERE h.disaster_id = :disaster_id AND h.external_id = :external_id
        """,
        disaster_id=disaster_id,
        external_id=BIHAR_AFFECTED_AREA_ID,
    )
    if item is None:
        return {"type": "FeatureCollection", "features": []}
    geometry = item.pop("geometry")
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": geometry, "properties": item}],
    }


@router.get("/map/depots")
def map_depots(db: Session = Depends(get_db)) -> dict[str, Any]:
    depots = _all(
        db,
        """
        SELECT id, name, district, state, ST_AsGeoJSON(geometry)::json AS geometry
        FROM resource_depots WHERE geometry IS NOT NULL ORDER BY name
        """,
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": item["id"],
                "geometry": item.pop("geometry"),
                "properties": item,
            }
            for item in depots
        ],
    }


@router.get("/map/operations")
def map_operations(
    disaster_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    disaster_id = _resolve_disaster_id(db, disaster_id)
    operations = _all(
        db,
        """
        SELECT id, team_name, operation_type, status, notes,
               ST_AsGeoJSON(location)::json AS geometry
        FROM field_operations
        WHERE disaster_id = :disaster_id AND location IS NOT NULL
        ORDER BY id
        """,
        disaster_id=disaster_id,
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": item["id"],
                "geometry": item.pop("geometry"),
                "properties": item,
            }
            for item in operations
        ],
    }


@router.get("/impact")
def impact(db: Session = Depends(get_db)) -> dict[str, Any]:
    summary = overview(db)
    zones_data = _all(
        db,
        """
        SELECT id, zone_code, severity_class, impact_score, population_count,
               building_count, building_footprint_area_km2, road_length_km,
               hazard_coverage, population_exposure, building_exposure,
               infrastructure_exposure, vulnerability_score
        FROM impact_zones WHERE disaster_id = :disaster_id ORDER BY zone_code
        """,
        disaster_id=summary["disaster"]["id"],
    )
    population_dataset = _one(
        db,
        """
        SELECT COALESCE(SUM(pd.valid_pixel_count), 0) AS valid_dataset_pixels
        FROM population_datasets pd
        JOIN disaster_sources ds ON ds.id = pd.source_id
        WHERE ds.disaster_id = :disaster_id
        """,
        disaster_id=summary["disaster"]["id"],
    )
    return {
        "population": summary["population"],
        "buildings": summary["buildings"],
        "zone_count": summary["zone_count"],
        "severity_distribution": summary["severity_distribution"],
        "valid_dataset_pixels": (
            population_dataset["valid_dataset_pixels"] if population_dataset else 0
        ),
        "zones": zones_data,
    }
