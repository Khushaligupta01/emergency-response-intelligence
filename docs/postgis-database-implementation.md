# PostGIS database implementation

The Week 1–2 database uses the configured PostgreSQL/PostGIS database and the project's SQLAlchemy 2.x models and Alembic migrations. Run Alembic from any working directory; configuration loads `backend/.env` and makes its `DATABASE_URL` available to Alembic. Do not commit the environment file or copy credentials into migration files.

## Schema

| Table | Purpose |
| --- | --- |
| `disasters` | Canonical event record with an EPSG:4326 event geometry. |
| `disaster_sources` | Source-specific external identifiers, URLs, input paths, hashes, and provenance. |
| `hazard_observations` | Event-linked vector/raster observations; `external_id` makes imported records repeatable. |
| `impact_zones` | Generated grid cells and impact/exposure metrics. Scores retain eight decimal places. |
| `building_exposure_footprints` | Individual OSM footprint portions from Step 2D clipped to the GDACS reported affected area. |
| `population_datasets` | WorldPop dataset provenance, licensing, raster dimensions, bounds, CRS, units, NoData, and summary statistics. It stores raster metadata, not millions of pixel rows. |
| `population_exposures` | Population and valid/affected-pixel totals linked to the source raster, disaster, and hazard observation. |
| `infrastructure_assets` | Source-attributed spatial asset inventory for future use; no records are loaded without an approved source dataset. |
| `resource_types` | Data-driven resource catalogue. |
| `resource_depots` | Depot attributes and EPSG:4326 point locations. |
| `inventory_balances` | Current on-hand and reserved amounts by depot and resource type. |
| `inventory_transactions` | Auditable receipt, consumption, transfer, and opening-balance ledger. |
| `field_operations` | Disaster-linked response operations with optional zone and location. |
| `source_ingestion_runs` | Per-source polling status, counts, response metadata, and errors. |
| `source_http_states` | ETag/Last-Modified validators and response cache headers; no response bodies. |
| `satellite_catalog_items` | Metadata-only Copernicus STAC item and remote asset references. |

All spatial columns use SRID 4326 for interoperability and have GiST indexes. Metric areas are calculated in the appropriate projected CRS (EPSG:32645 for this Bihar assessment); stored longitude/latitude geometries are not used for metric area calculations. Foreign keys, unique keys, timestamps, and range/geometry checks protect relationships and values. Alembic migrations are additive and preserve existing inventory and zone data.

## Loaded data

`scripts/processing/load_step4_data.py` idempotently loads the existing validated data:

- GDACS event `1104121`, Episode `20`, `Poly_Affected`, as source provenance and a flood `hazard_observations` geometry.
- The 198,182 individual OSM building footprints clipped to that reported affected area. The table geometry therefore represents the clipped exposure portion, not the full original building footprint.
- WorldPop 2020 raster metadata and the Step 2D population exposure summary. Raster pixels remain in the validated GeoTIFF rather than being expanded into millions of database records.
- The 1,072 Step 3 grid zones, upserted by disaster and zone code.

The GDACS geometry is an analyst-reported affected area, not an observed satellite-derived inundation extent. WorldPop values are modelled estimated people per pixel, not density. OSM coverage is incomplete. No infrastructure records or operational resource/depot data are fabricated or seeded.

## Migrations and validation

From the repository root:

```powershell
.\backend\.venv\Scripts\alembic.exe -c .\backend\alembic.ini upgrade head
.\backend\.venv\Scripts\python.exe .\scripts\processing\load_step4_data.py
.\backend\.venv\Scripts\python.exe .\scripts\processing\validate_step4_database.py
Push-Location .\backend
.\.venv\Scripts\python.exe -m pytest -q
Pop-Location
```

The loader checks source metadata, geometry identity, CRS, raster values, counts, and input semantics before committing a single transaction. The independent database validator checks schema, EPSG:4326 geometry metadata, GiST indexes, source lineage, loaded record counts, and representative spatial queries. `backend/tests/test_postgis_schema.py` exercises the live connection, PostGIS, SRIDs, spatial indexes and queries, foreign keys, and check constraints; its writes are rolled back.

Official GDACS, USGS, NDMA/SACHET CAP, and Copernicus STAC ingestion is implemented separately from the Step 4 data loader. See [the source integration documentation](./disaster-source-integration.md) for endpoints, refresh behavior, provenance, and limitations.
