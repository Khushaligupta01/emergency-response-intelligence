@"
# AI-Powered Emergency Response Intelligence Platform

An AI-powered full-stack platform for disaster management, emergency resource planning, demand prediction, logistics optimization, and operational decision support.

## Project Status

The backend foundation, real-data geospatial impact assessment, geographic zone classification baseline, transactional resource inventory, PostGIS database, and official GDACS/USGS/SACHET ingestion with Copernicus metadata discovery are implemented. Satellite damage estimation, demand prediction, optimization, routing, and operational dashboards remain out of scope for the current phase.

The repository also contains the frontend scaffold and infrastructure configuration for later development phases.

### Resource inventory

The backend resource inventory uses configurable resource types, PostGIS depot locations, transaction-backed stock balances, and an auditable receipt/consumption/transfer ledger. See [the inventory design and usage notes](./docs/resource-inventory.md).

### Geographic zone classification

The Step 3 grid aggregates the existing GDACS reported affected area, OSM building impacts, and WorldPop estimates in a projected Bihar grid. Its configurable severity score is an engineering baseline, not a validated domain model. See [the geographic zone classification methodology](./docs/geographic-zone-classification.md).

### PostGIS database

The Week 1–2 schema stores disaster/source provenance, hazard geometries, impact zones, population raster metadata and exposure summaries, OSM building exposure footprints, infrastructure inventory structure, resource inventory, and field operations. See [the PostGIS schema and data-loading notes](./docs/postgis-database-implementation.md).

### Multi-source disaster ingestion

Official GDACS, USGS, NDMA/SACHET CAP, and Copernicus STAC metadata ingestion is documented in [the source integration notes](./docs/disaster-source-integration.md). Polling and catalogue discovery are implemented; satellite damage estimation is not.

## Planned Capabilities

- Automated source-refresh scheduling and monitoring
- Geospatial impact assessment
- Geographic grid and severity classification baseline
- Auditable resource inventory management
- Disaster zone classification
- Population and vulnerability analysis
- Resource demand prediction
- Resource allocation optimization
- Logistics and route planning
- Emergency response command dashboard
- Field mission management
- Post-event analytics

## Architecture

Disaster & Geospatial Data
→ Impact Assessment
→ Demand Prediction
→ Resource Allocation & Logistics
→ Operations Command Dashboard

## Technology Stack

### Frontend
- React
- TypeScript
- Vite
- Tailwind CSS
- Leaflet
- Recharts
- Axios
- Zustand

### Backend
- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic

### Database
- PostgreSQL
- PostGIS

### AI / Machine Learning
- NumPy
- Pandas
- Scikit-learn
- XGBoost
- LightGBM

### Optimization
- Google OR-Tools

### Geospatial
- GeoPandas
- Shapely
- PyProj
- Rasterio
- Pyogrio
- OSMnx

### Infrastructure
- Docker
- Redis
- Celery
- GitHub Actions
- AWS

## Project Structure

```text
emergency-response-intelligence/
├── backend/
├── frontend/
├── ml/
├── data/
├── infrastructure/
├── scripts/
├── tests/
├── docs/
├── docker-compose.yml
├── .dockerignore
├── .env.example
├── .gitignore
└── README.md