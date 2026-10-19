from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
import json
import math
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET

import httpx
import shapely
from pyproj import Geod
from shapely.geometry import Polygon, mapping, shape


GDACS_API = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
USGS_ALL_DAY_FEED = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
SACHET_BIHAR_FEED = "https://sachet.ndma.gov.in/cap_public_website/rss/rss_bihar.xml"
SACHET_HOST = "sachet.ndma.gov.in"
SACHET_CAP_PREFIX = "/cap_public_website/FetchXMLFile"
COPERNICUS_STAC_SEARCH = "https://stac.dataspace.copernicus.eu/v1/search"
WGS84_GEOD = Geod(ellps="WGS84")


class SourceIntegrationError(RuntimeError):
    pass


class SourcePayloadError(ValueError):
    pass


@dataclass(frozen=True)
class NormalizedEvent:
    source_name: str
    external_id: str
    event_type: str
    name: str
    occurred_at: datetime | None
    expires_at: datetime | None
    severity: str | None
    country: str | None
    geometry: dict[str, Any] | None
    source_url: str | None
    observation_id: str
    observed_at: datetime | None
    metadata: dict[str, Any]


@dataclass
class FetchResult:
    source_name: str
    request_url: str
    events: list[NormalizedEvent] = field(default_factory=list)
    items_seen: int = 0
    items_failed: int = 0
    errors: list[str] = field(default_factory=list)
    response_metadata: dict[str, Any] = field(default_factory=dict)
    not_modified: bool = False
    satellite_items: list[dict[str, Any]] = field(default_factory=list)


def parse_datetime(value: Any, *, default_timezone=timezone.utc) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            raise SourcePayloadError("Timestamp must be finite.")
        if abs(number) > 100_000_000_000:
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise SourcePayloadError("Timestamp is outside the supported range.") from error
    if not isinstance(value, str):
        raise SourcePayloadError("Timestamp must be an ISO string or epoch value.")
    text_value = value.strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.strptime(
                text_value,
                "%a %b %d %H:%M:%S IST %Y",
            ).replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        except ValueError as error:
            try:
                parsed_date = date.fromisoformat(text_value)
                parsed = datetime.combine(parsed_date, time.min)
            except ValueError:
                raise SourcePayloadError(f"Unsupported timestamp format: {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_timezone)
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SourcePayloadError(f"{label} must be numeric.")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise SourcePayloadError(f"{label} must be numeric.") from error
    if not math.isfinite(result):
        raise SourcePayloadError(f"{label} must be finite.")
    return result


def normalize_geometry(geometry: Any, *, required=False) -> dict[str, Any] | None:
    if geometry is None:
        if required:
            raise SourcePayloadError("Feature geometry is required.")
        return None
    if not isinstance(geometry, dict) or not geometry.get("type"):
        raise SourcePayloadError("Geometry must be a GeoJSON geometry object.")
    try:
        parsed = shape(geometry)
        if parsed.is_empty or not parsed.is_valid:
            raise SourcePayloadError("Geometry is empty or invalid.")
        parsed = shapely.force_2d(parsed)
        if parsed.is_empty or not parsed.is_valid:
            raise SourcePayloadError("Geometry is invalid after removing source Z coordinates.")
        min_x, min_y, max_x, max_y = parsed.bounds
        if min_x < -180 or max_x > 180 or min_y < -90 or max_y > 90:
            raise SourcePayloadError("GeoJSON geometry is outside EPSG:4326 coordinate bounds.")
        return mapping(parsed)
    except (TypeError, ValueError, shapely.errors.GEOSException) as error:
        if isinstance(error, SourcePayloadError):
            raise
        raise SourcePayloadError(f"Unable to parse GeoJSON geometry: {error}") from error


def _valid_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    return value.strip()


def parse_gdacs_feature(feature: Any) -> NormalizedEvent:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise SourcePayloadError("GDACS record must be a GeoJSON Feature.")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise SourcePayloadError("GDACS feature properties are missing.")
    event_id = properties.get("eventid")
    event_type = properties.get("eventtype")
    if event_id is None or not event_type:
        raise SourcePayloadError("GDACS event ID and event type are required.")
    external_id = str(event_id).strip()
    event_type = str(event_type).strip().upper()
    if not external_id:
        raise SourcePayloadError("GDACS event ID cannot be blank.")
    episode_id = properties.get("episodeid")
    source_url = properties.get("url")
    if isinstance(source_url, dict):
        source_url = source_url.get("report") or source_url.get("geometry")
    name = properties.get("name") or properties.get("eventname") or f"GDACS {event_type} {external_id}"
    start_time = parse_datetime(properties.get("fromdate"))
    end_time = parse_datetime(properties.get("todate"))
    if start_time and end_time and end_time < start_time:
        raise SourcePayloadError("GDACS event ends before its start.")
    geometry = normalize_geometry(feature.get("geometry"))
    if geometry is None:
        raise SourcePayloadError("GDACS event geometry is missing.")
    observation_id = ":".join(
        part for part in (event_type, external_id, str(episode_id) if episode_id is not None else "") if part
    )
    return NormalizedEvent(
        source_name="GDACS",
        external_id=external_id,
        event_type=event_type,
        name=str(name),
        occurred_at=start_time,
        expires_at=end_time,
        severity=str(properties["alertlevel"]) if properties.get("alertlevel") else None,
        country=str(properties["country"]) if properties.get("country") else None,
        geometry=geometry,
        source_url=_valid_url(source_url),
        observation_id=observation_id,
        observed_at=parse_datetime(properties.get("datemodified")),
        metadata={
            "api": GDACS_API,
            "event_id": external_id,
            "event_type": event_type,
            "episode_id": episode_id,
            "source_updated_at": properties.get("datemodified"),
            "alert_score": properties.get("alertscore"),
            "episode_alert_level": properties.get("episodealertlevel"),
            "episode_alert_score": properties.get("episodealertscore"),
            "source": properties.get("source"),
            "source_id": properties.get("sourceid"),
            "polygon_label": properties.get("polygonlabel"),
            "geometry_class": properties.get("Class"),
            "raw": properties,
        },
    )


def parse_usgs_feature(feature: Any) -> NormalizedEvent:
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise SourcePayloadError("USGS record must be a GeoJSON Feature.")
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise SourcePayloadError("USGS feature properties are missing.")
    event_id = feature.get("id")
    geometry = normalize_geometry(feature.get("geometry"), required=True)
    coordinates = geometry.get("coordinates")
    if geometry.get("type") != "Point" or not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
        raise SourcePayloadError("USGS earthquake geometry must be a longitude/latitude point.")
    longitude = _finite_number(coordinates[0], "longitude")
    latitude = _finite_number(coordinates[1], "latitude")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise SourcePayloadError("USGS longitude or latitude is outside valid bounds.")
    magnitude = properties.get("mag")
    if magnitude is not None:
        _finite_number(magnitude, "magnitude")
    depth = None
    raw_coordinates = feature["geometry"].get("coordinates")
    if isinstance(raw_coordinates, list) and len(raw_coordinates) >= 3:
        depth = _finite_number(raw_coordinates[2], "depth_km")
    occurred_at = parse_datetime(properties.get("time"))
    if not event_id or occurred_at is None:
        raise SourcePayloadError("USGS event ID and event timestamp are required.")
    metadata = dict(properties)
    metadata["depth_km"] = depth
    metadata["feed_url"] = USGS_ALL_DAY_FEED
    return NormalizedEvent(
        source_name="USGS",
        external_id=str(event_id),
        event_type=str(properties.get("type") or "earthquake"),
        name=str(properties.get("title") or properties.get("place") or f"Earthquake {event_id}"),
        occurred_at=occurred_at,
        expires_at=None,
        severity=str(magnitude) if magnitude is not None else None,
        country=None,
        geometry=geometry,
        source_url=_valid_url(properties.get("url") or properties.get("detail")),
        observation_id=str(event_id),
        observed_at=parse_datetime(properties.get("updated")),
        metadata=metadata,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def _cap_text(element: ET.Element, name: str, default: str | None = None) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == name.lower():
            return child.text.strip() if child.text and child.text.strip() else default
    return default


def _cap_info(root: ET.Element) -> ET.Element | None:
    info_elements = [child for child in list(root) if _local_name(child.tag) == "info"]
    if not info_elements:
        return None
    for info in info_elements:
        if (_cap_text(info, "language") or "").lower().startswith("en"):
            return info
    return info_elements[0]


def _parse_cap_geometry(info: ET.Element) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    polygons: list[Polygon] = []
    circles: list[Polygon] = []
    area_metadata: list[dict[str, str]] = []
    for area in (child for child in info.iter() if _local_name(child.tag) == "area"):
        area_metadata.append(
            {
                key: value
                for tag in list(area)
                if (key := _local_name(tag.tag)) in ("areadesc", "polygon", "circle", "geocode")
                and (value := (tag.text or "").strip())
            }
        )
        for child in area.iter():
            tag = _local_name(child.tag)
            text_value = (child.text or "").strip()
            if tag == "polygon" and text_value:
                points = []
                for token in text_value.split():
                    pair = token.split(",")
                    if len(pair) != 2:
                        raise SourcePayloadError("CAP polygon coordinates must be 'latitude,longitude'.")
                    latitude = _finite_number(pair[0], "CAP polygon latitude")
                    longitude = _finite_number(pair[1], "CAP polygon longitude")
                    points.append((longitude, latitude))
                if len(points) >= 3:
                    if points[0] != points[-1]:
                        points.append(points[0])
                    polygon = Polygon(points)
                    if not polygon.is_valid or polygon.is_empty:
                        raise SourcePayloadError("CAP area polygon is invalid.")
                    polygons.append(polygon)
            elif tag == "circle" and text_value:
                parts = text_value.split()
                if len(parts) != 2:
                    raise SourcePayloadError("CAP circle must contain a centre and radius in kilometres.")
                pair = parts[0].split(",")
                if len(pair) != 2:
                    raise SourcePayloadError("CAP circle centre must be 'latitude,longitude'.")
                latitude = _finite_number(pair[0], "CAP circle latitude")
                longitude = _finite_number(pair[1], "CAP circle longitude")
                radius_km = _finite_number(parts[1], "CAP circle radius")
                if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                    raise SourcePayloadError("CAP circle centre is outside valid longitude/latitude bounds.")
                if radius_km <= 0:
                    raise SourcePayloadError("CAP circle radius must be positive.")
                bearings = list(range(0, 360, 2))
                longitudes, latitudes, _ = WGS84_GEOD.fwd(
                    [longitude] * len(bearings),
                    [latitude] * len(bearings),
                    bearings,
                    [radius_km * 1000] * len(bearings),
                )
                circles.append(
                    Polygon(
                        list(zip(longitudes, latitudes, strict=True))
                        + [(longitudes[0], latitudes[0])]
                    )
                )
    geometries = polygons + circles
    if geometries:
        geometry = shapely.union_all(geometries)
        return normalize_geometry(mapping(geometry), required=True), area_metadata
    return None, area_metadata


def parse_sachet_cap(xml_payload: bytes | str, *, source_url: str, feed_guid: str | None = None) -> NormalizedEvent:
    try:
        root = ET.fromstring(xml_payload)
    except ET.ParseError as error:
        raise SourcePayloadError(f"SACHET CAP XML is malformed: {error}") from error
    if _local_name(root.tag) != "alert":
        raise SourcePayloadError("SACHET CAP document root must be alert.")
    info = _cap_info(root)
    if info is None:
        raise SourcePayloadError("SACHET CAP document has no info section.")
    external_id = _cap_text(root, "identifier")
    sent = parse_datetime(_cap_text(root, "sent"))
    effective = parse_datetime(_cap_text(info, "effective")) or sent
    expires = parse_datetime(_cap_text(info, "expires"))
    if not external_id or effective is None:
        raise SourcePayloadError("SACHET CAP identifier and effective/sent timestamp are required.")
    if expires and effective and expires < effective:
        raise SourcePayloadError("SACHET CAP alert expires before its effective time.")
    geometry, areas = _parse_cap_geometry(info)
    source_link = _valid_url(source_url)
    if source_link is None or urlparse(source_link).hostname != SACHET_HOST:
        raise SourcePayloadError("SACHET CAP URL must use the official sachet.ndma.gov.in host.")
    event_type = _cap_text(info, "event") or "unknown"
    headline = _cap_text(info, "headline") or event_type
    raw = {
        "identifier": external_id,
        "sender": _cap_text(root, "sender"),
        "sent": _cap_text(root, "sent"),
        "status": _cap_text(root, "status"),
        "msg_type": _cap_text(root, "msgtype"),
        "scope": _cap_text(root, "scope"),
        "language": _cap_text(info, "language"),
        "category": _cap_text(info, "category"),
        "event": event_type,
        "urgency": _cap_text(info, "urgency"),
        "severity": _cap_text(info, "severity"),
        "certainty": _cap_text(info, "certainty"),
        "effective": _cap_text(info, "effective"),
        "onset": _cap_text(info, "onset"),
        "expires": _cap_text(info, "expires"),
        "headline": headline,
        "description": _cap_text(info, "description"),
        "instruction": _cap_text(info, "instruction"),
        "areas": areas,
        "xml": xml_payload.decode("utf-8") if isinstance(xml_payload, bytes) else xml_payload,
        "feed_guid": feed_guid,
    }
    return NormalizedEvent(
        source_name="SACHET",
        external_id=external_id,
        event_type=event_type,
        name=str(headline),
        occurred_at=effective,
        expires_at=expires,
        severity=_cap_text(info, "severity"),
        country="India",
        geometry=geometry,
        source_url=source_link,
        observation_id=external_id,
        observed_at=sent,
        metadata=raw,
    )


def parse_sachet_rss(payload: bytes | str) -> list[dict[str, str | None]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise SourcePayloadError(f"SACHET RSS feed is malformed: {error}") from error
    if _local_name(root.tag) != "rss":
        raise SourcePayloadError("SACHET response is not an RSS feed.")
    items = []
    for item in (element for element in root.iter() if _local_name(element.tag) == "item"):
        values: dict[str, str | None] = {}
        for child in item:
            key = _local_name(child.tag)
            if key in ("link", "guid", "title", "pubdate"):
                values[key] = child.text.strip() if child.text else None
        link = _valid_url(values.get("link"))
        if not link or urlparse(link).hostname != SACHET_HOST:
            raise SourcePayloadError("SACHET RSS item has a missing or non-official CAP link.")
        if SACHET_CAP_PREFIX not in urlparse(link).path:
            raise SourcePayloadError("SACHET RSS item link is not a CAP message URL.")
        values["link"] = link
        if not values.get("guid"):
            values["guid"] = link.rsplit("=", 1)[-1]
        items.append(values)
    return items


def parse_stac_item(item: Any, *, collection_id: str) -> dict[str, Any]:
    if not isinstance(item, dict) or item.get("type") != "Feature":
        raise SourcePayloadError("Copernicus STAC item must be a GeoJSON Feature.")
    item_id = item.get("id")
    properties = item.get("properties")
    if not item_id or not isinstance(properties, dict):
        raise SourcePayloadError("Copernicus STAC item ID or properties are missing.")
    geometry = normalize_geometry(item.get("geometry"), required=True)
    acquired_at = parse_datetime(properties.get("datetime"))
    if acquired_at is None:
        raise SourcePayloadError("Copernicus STAC item has no acquisition datetime.")
    cloud_cover = properties.get("eo:cloud_cover")
    if cloud_cover is not None:
        cloud_cover = _finite_number(cloud_cover, "eo:cloud_cover")
        if not 0 <= cloud_cover <= 100:
            raise SourcePayloadError("eo:cloud_cover is outside [0,100].")
    bbox = item.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        raise SourcePayloadError("Copernicus STAC item has no valid bounding box.")
    numeric_bbox = [_finite_number(value, "STAC bbox coordinate") for value in bbox]
    if numeric_bbox[0] > numeric_bbox[2] or numeric_bbox[1] > numeric_bbox[3]:
        raise SourcePayloadError("Copernicus STAC bounding box is inverted.")
    assets: dict[str, Any] = {}
    raw_assets = item.get("assets") or {}
    if not isinstance(raw_assets, dict):
        raise SourcePayloadError("Copernicus STAC assets must be an object.")
    for name, asset in raw_assets.items():
        if not isinstance(asset, dict):
            continue
        assets[name] = {
            key: asset[key]
            for key in ("href", "type", "title", "roles", "file:size")
            if asset.get(key) is not None
        }
        alternate = asset.get("alternate")
        if isinstance(alternate, dict):
            https = alternate.get("https")
            if isinstance(https, dict) and https.get("href"):
                assets[name]["https_href"] = https["href"]
    links = item.get("links") or []
    if not isinstance(links, list):
        raise SourcePayloadError("Copernicus STAC links must be an array.")
    self_link = next(
        (link.get("href") for link in links if isinstance(link, dict) and link.get("rel") == "self"),
        None,
    )
    return {
        "external_id": str(item_id),
        "collection_id": str(item.get("collection") or collection_id),
        "acquired_at": acquired_at,
        "geometry": geometry,
        "bbox": numeric_bbox,
        "cloud_cover": cloud_cover,
        "item_url": _valid_url(self_link),
        "assets": assets,
        "metadata": {
            key: value
            for key, value in properties.items()
            if key not in ("thumbnail",)
        },
    }


def decode_json_response(response: httpx.Response, *, source_name: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as error:
        raise SourcePayloadError(f"{source_name} response is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise SourcePayloadError(f"{source_name} response root must be an object.")
    return payload


def _safe_response_metadata(response: httpx.Response) -> dict[str, Any]:
    return {
        "url": str(response.request.url),
        "method": response.request.method,
        "status_code": response.status_code,
        "etag": response.headers.get("ETag"),
        "last_modified": response.headers.get("Last-Modified"),
        "cache_control": response.headers.get("Cache-Control"),
        "date": response.headers.get("Date"),
        "content_type": response.headers.get("Content-Type"),
    }


def _require_success(response: httpx.Response, source_name: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise SourceIntegrationError(
            f"{source_name} returned HTTP {response.status_code} for {response.request.url}."
        ) from error


def fetch_gdacs(
    client: httpx.Client,
    *,
    from_date: datetime,
    to_date: datetime,
    validator_headers: dict[str, str] | None = None,
) -> FetchResult:
    if to_date < from_date:
        raise ValueError("GDACS query end time must not precede its start time.")
    url = GDACS_API
    params = {
        "fromdate": from_date.astimezone(timezone.utc).date().isoformat(),
        "todate": to_date.astimezone(timezone.utc).date().isoformat(),
    }
    request_url = str(httpx.URL(url, params=params))
    try:
        response = client.get(url, params=params, headers=validator_headers or {})
        if response.status_code == 304:
            return FetchResult(
                "GDACS",
                request_url,
                not_modified=True,
                response_metadata={"responses": [_safe_response_metadata(response)]},
            )
        _require_success(response, "GDACS")
    except httpx.HTTPError as error:
        raise SourceIntegrationError(f"GDACS request failed: {error}") from error
    payload = decode_json_response(response, source_name="GDACS")
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise SourcePayloadError("GDACS API response must be a GeoJSON FeatureCollection.")
    result = FetchResult(
        "GDACS",
        request_url,
        items_seen=len(payload["features"]),
        response_metadata={
            "responses": [_safe_response_metadata(response)],
            "api_metadata": payload.get("metadata"),
            "query": params,
        },
    )
    for feature in payload["features"]:
        try:
            event = parse_gdacs_feature(feature)
            result.events.append(
                NormalizedEvent(
                    **{
                        **event.__dict__,
                        "metadata": {
                            **event.metadata,
                            "api_response_generated": payload.get("metadata", {}).get("generated")
                            if isinstance(payload.get("metadata"), dict)
                            else None,
                            "query": params,
                        },
                    }
                )
            )
        except (SourcePayloadError, TypeError, ValueError) as error:
            result.items_failed += 1
            result.errors.append(f"GDACS feature: {error}")
    return result


def fetch_usgs(
    client: httpx.Client,
    *,
    validator_headers: dict[str, str] | None = None,
) -> FetchResult:
    try:
        response = client.get(USGS_ALL_DAY_FEED, headers=validator_headers or {})
        if response.status_code == 304:
            return FetchResult(
                "USGS",
                USGS_ALL_DAY_FEED,
                not_modified=True,
                response_metadata={"responses": [_safe_response_metadata(response)]},
            )
        _require_success(response, "USGS")
    except httpx.HTTPError as error:
        raise SourceIntegrationError(f"USGS request failed: {error}") from error
    payload = decode_json_response(response, source_name="USGS")
    if payload.get("type") != "FeatureCollection" or not isinstance(payload.get("features"), list):
        raise SourcePayloadError("USGS response must be a GeoJSON FeatureCollection.")
    feed_metadata = payload.get("metadata")
    if not isinstance(feed_metadata, dict):
        feed_metadata = {}
    result = FetchResult(
        "USGS",
        USGS_ALL_DAY_FEED,
        items_seen=len(payload["features"]),
        response_metadata={
            "responses": [_safe_response_metadata(response)],
            "feed_title": feed_metadata.get("title"),
            "feed_generated": feed_metadata.get("generated"),
        },
    )
    for feature in payload["features"]:
        try:
            result.events.append(parse_usgs_feature(feature))
        except (SourcePayloadError, TypeError, ValueError) as error:
            result.items_failed += 1
            result.errors.append(f"USGS feature: {error}")
    return result


def fetch_sachet(
    client: httpx.Client,
    *,
    feed_url: str = SACHET_BIHAR_FEED,
    validator_headers: dict[str, str] | None = None,
    validator_for_url=None,
) -> FetchResult:
    parsed_feed_url = urlparse(feed_url)
    if parsed_feed_url.scheme != "https" or parsed_feed_url.hostname != SACHET_HOST:
        raise ValueError("SACHET feed URL must use HTTPS on sachet.ndma.gov.in.")
    try:
        feed_response = client.get(feed_url, headers=validator_headers or {})
        if feed_response.status_code == 304:
            return FetchResult(
                "SACHET",
                feed_url,
                not_modified=True,
                response_metadata={"responses": [_safe_response_metadata(feed_response)]},
            )
        _require_success(feed_response, "SACHET RSS")
    except httpx.HTTPError as error:
        raise SourceIntegrationError(f"SACHET RSS request failed: {error}") from error
    content_type = feed_response.headers.get("Content-Type", "").lower()
    if "xml" not in content_type and not feed_response.content.lstrip().startswith(b"<?xml"):
        raise SourcePayloadError("SACHET state feed did not return XML.")
    items = parse_sachet_rss(feed_response.content)
    result = FetchResult(
        "SACHET",
        feed_url,
        items_seen=len(items),
        response_metadata={"responses": [_safe_response_metadata(feed_response)]},
    )
    for item in items:
        link = str(item["link"])
        headers = validator_for_url(link) if validator_for_url else {}
        try:
            cap_response = client.get(link, headers=headers)
            if cap_response.status_code == 304:
                result.response_metadata["responses"].append(_safe_response_metadata(cap_response))
                continue
            _require_success(cap_response, "SACHET CAP")
            if "xml" not in cap_response.headers.get("Content-Type", "").lower() and not cap_response.content.lstrip().startswith(b"<"):
                raise SourcePayloadError("SACHET CAP URL did not return XML.")
            event = parse_sachet_cap(
                cap_response.content,
                source_url=link,
                feed_guid=item.get("guid"),
            )
            result.events.append(event)
            result.response_metadata["responses"].append(_safe_response_metadata(cap_response))
        except (httpx.HTTPError, SourceIntegrationError, SourcePayloadError, TypeError, ValueError) as error:
            result.items_failed += 1
            result.errors.append(f"SACHET CAP {item.get('guid')}: {error}")
            result.response_metadata["responses"].append(
                {
                    "url": link,
                    "status_code": getattr(getattr(error, "response", None), "status_code", None),
                    "error": str(error),
                }
            )
    return result


def fetch_copernicus_stac(
    client: httpx.Client,
    *,
    bbox: list[float],
    geometry: Any,
    start: datetime,
    end: datetime,
    max_items_per_collection: int = 25,
    sentinel2_max_cloud: float = 30.0,
) -> FetchResult:
    if len(bbox) != 4 or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
        raise ValueError("Copernicus STAC search requires a valid WGS84 bounding box.")
    if end < start:
        raise ValueError("Copernicus STAC search end time must not precede start time.")
    if max_items_per_collection < 1:
        raise ValueError("Copernicus STAC result limit must be positive.")
    if not 0 <= sentinel2_max_cloud <= 100:
        raise ValueError("Sentinel-2 cloud-cover limit must be in [0,100].")
    try:
        hazard = shape(geometry) if isinstance(geometry, dict) else geometry
    except (TypeError, ValueError, shapely.errors.GEOSException) as error:
        raise SourcePayloadError(f"Invalid Copernicus study geometry: {error}") from error
    if hazard.is_empty or not hazard.is_valid:
        raise SourcePayloadError("Copernicus study geometry is empty or invalid.")
    payload_base: dict[str, Any] = {
        "bbox": bbox,
        "datetime": (
            f"{start.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}/"
            f"{end.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')}"
        ),
        "limit": max_items_per_collection,
    }
    result = FetchResult(
        "COPERNICUS",
        COPERNICUS_STAC_SEARCH,
        response_metadata={
            "endpoint": COPERNICUS_STAC_SEARCH,
            "search_bbox_epsg4326": bbox,
            "datetime": payload_base["datetime"],
            "collections": [],
            "sentinel2_max_cloud_cover": sentinel2_max_cloud,
            "asset_downloaded": False,
        },
    )
    for collection_id in ("sentinel-1-grd", "sentinel-2-l2a"):
        payload = {**payload_base, "collections": [collection_id]}
        if collection_id == "sentinel-2-l2a":
            payload["query"] = {"eo:cloud_cover": {"lte": sentinel2_max_cloud}}
        next_token = None
        collected_for_collection = 0
        page_number = 0
        while collected_for_collection < max_items_per_collection and page_number < 4:
            if next_token is not None:
                payload["token"] = next_token
            try:
                response = client.post(COPERNICUS_STAC_SEARCH, json=payload)
                _require_success(response, "Copernicus STAC")
            except httpx.HTTPError as error:
                raise SourceIntegrationError(f"Copernicus STAC search failed: {error}") from error
            page = decode_json_response(response, source_name="Copernicus STAC")
            if page.get("type") != "FeatureCollection" or not isinstance(page.get("features"), list):
                raise SourcePayloadError("Copernicus STAC response must be a FeatureCollection.")
            page_links = page.get("links", [])
            if not isinstance(page_links, list):
                raise SourcePayloadError("Copernicus STAC links must be an array.")
            result.response_metadata.setdefault("responses", []).append(_safe_response_metadata(response))
            page_number += 1
            page_features = page["features"]
            result.items_seen += len(page_features)
            for item in page_features:
                try:
                    normalized = parse_stac_item(item, collection_id=collection_id)
                    if not hazard.intersects(shape(normalized["geometry"])):
                        continue
                    result.satellite_items.append(normalized)
                    collected_for_collection += 1
                    if collected_for_collection >= max_items_per_collection:
                        break
                except (SourcePayloadError, TypeError, ValueError) as error:
                    result.items_failed += 1
                    result.errors.append(f"Copernicus {collection_id} item: {error}")
            next_link = next(
                (link for link in page_links if isinstance(link, dict) and link.get("rel") == "next"),
                None,
            )
            next_token = (
                (next_link.get("body") or {}).get("token")
                if isinstance(next_link, dict)
                else None
            )
            if not next_token or not page_features:
                break
        result.response_metadata["collections"].append(
            {
                "collection_id": collection_id,
                "selected_count": collected_for_collection,
                "pages_fetched": page_number,
            }
        )
    return result
