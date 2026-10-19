# Multi-source disaster data integration

The Week 1–2 ingestion layer normalizes official source records into the existing
`disasters`, `disaster_sources`, and `hazard_observations` tables. Every polling
attempt is recorded in `source_ingestion_runs`; HTTP validators and response
cache headers are recorded in `source_http_states`. Copernicus catalogue
metadata is stored in `satellite_catalog_items`. Response bodies are not stored
in the HTTP-state table.

## Sources and extracted fields

| Source | Official mechanism | Normalized information |
| --- | --- | --- |
| GDACS | [GDACS GeoJSON event search](https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH) | Event ID and type, episode ID, name, alert level/score, country, start/end/modified timestamps, event geometry, report URL, and raw source properties. |
| USGS | [USGS GeoJSON feeds](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php) | USGS event ID, event time, magnitude, title/place, longitude/latitude point, depth in kilometres, event URL, and source properties. |
| NDMA/SACHET | [Official Bihar RSS CAP feed](https://sachet.ndma.gov.in/cap_public_website/rss/rss_bihar.xml) linking to the official CAP XML `FetchXMLFile` endpoint | CAP identifier, event/category, severity, urgency, certainty, sent/effective/onset/expiry times, headline, description, area description, and any CAP polygon/circle geometry. Missing CAP geometry remains null; the pipeline does not infer a boundary from text. |
| Copernicus | [Copernicus Data Space STAC API](https://stac.dataspace.copernicus.eu/v1/search) | Sentinel-1 GRD and Sentinel-2 L2A item identifiers, acquisition time, geometry, bounding box, cloud cover where provided, item URL, asset references, and catalogue properties. No assets are downloaded. |

The GDACS event-search response is not assumed to contain only the requested
hazard type: returned `eventtype` values are parsed from each feature and
preserved. Each GDACS episode is recorded as a distinct hazard observation.
Existing disaster geometries are retained on repeated ingestion when present,
so an event-list point/centroid cannot replace the Step 2B Bihar
`Poly_Affected` geometry.

USGS does not provide a country field in the feed; the canonical record stores
`Unknown (not provided by source)` and flags this omission in source metadata.
USGS depth remains in the raw/normalized metadata rather than being mistaken
for a 2D PostGIS geometry coordinate.

## Refresh, caching, and failure behavior

Run a scheduled poll from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe .\scripts\ingest_disaster_sources.py
```

By default, GDACS is queried for the last seven days, USGS uses its official
all-day feed, and SACHET uses the official Bihar RSS-to-CAP mechanism. The
Copernicus search is tied to the already-loaded GDACS event `1104121`, Episode
20, `Poly_Affected` geometry and stored event dates. Use `--days`,
`--from-date`, `--to-date`, or `--skip-copernicus` to adjust a polling run.
The script is a poller, not a push subscription or real-time alert service.

The SACHET feed publishes ETag/Last-Modified validators while its observed
response uses `Cache-Control: no-store`. The application stores validators and
response headers only, sends conditional requests when validators are
available, and does not cache the RSS/CAP response body in the database.
HTTP/API errors and malformed individual records are recorded in ingestion-run
status/error fields rather than reported as successful ingestion. Repeated
event and catalogue ingestion uses source/external-ID uniqueness and database
upserts to avoid duplicate records.

Copernicus search uses the event bounding box because the public STAC endpoint
rejects the detailed polygon `intersects` request. Returned item geometries
are then tested for exact intersection with the selected affected geometry
locally. Sentinel-2 items are filtered to `eo:cloud_cover <= 30`; Sentinel-1
GRD does not have that optical cloud filter. The CLI stores only catalogue
metadata and remote asset references. Imagery processing, flood delineation
from satellite data, and damage estimation are not implemented by this step.
Public STAC metadata discovery works without authentication; authenticated
access may be required to download some referenced assets, which this pipeline
does not attempt.

## Testing and fixtures

Offline tests use compact feature/CAP/STAC records captured from the official
endpoints under `backend/tests/fixtures/source_ingestion/`. These are test
fixtures only; the ingestion pipeline does not seed them as operational
records. Error tests exercise malformed payloads, HTTP failures, and `304 Not
Modified` handling without depending on a live source.

## Limitations

- Source refresh is scheduled polling; freshness depends on source publication
  and the polling schedule.
- GDACS feed geometry may be a point/centroid or a generalized event extent.
  It is not a substitute for the selected Step 2B analyst-reported affected
  area.
- SACHET CAP alerts may include only textual area descriptions; no geometry is
  created from those descriptions.
- Copernicus results are catalogue discoveries, not confirmed observations of
  flooding or damage. Assets are not downloaded or analyzed.
- Public feed availability, retention, response shape, and authentication
  requirements can change; failed and partial runs remain visible in the
  database.
