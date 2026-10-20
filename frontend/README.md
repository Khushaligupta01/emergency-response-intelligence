# Emergency Response dashboard

The frontend is a React, TypeScript, and Vite operational dashboard backed by the project's FastAPI and PostgreSQL/PostGIS data. It does not ship sample disaster values or stock balances.

## Run locally

1. Start PostgreSQL with PostGIS and apply the backend migrations/load the validated data as described in [`../docs/postgis-database-implementation.md`](../docs/postgis-database-implementation.md).
2. From the repository root, start the API:

   ```powershell
   Push-Location .\backend
   .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
   ```

3. In another terminal, start the frontend:

   ```powershell
   Push-Location .\frontend
   npm install
   npm run dev
   ```

4. Open the local Vite address, normally `http://localhost:5173`.

The API defaults to `http://127.0.0.1:8000`. Copy `.env.example` to `.env.local` and set `VITE_API_BASE_URL` only if the API is hosted elsewhere. Do not commit `.env.local`.

## Dashboard pages

- **Overview** — selected GDACS Bihar event, persisted population/building exposure, impact-zone severity counts, and source-ingestion runs.
- **Disaster map** — GDACS-reported affected area, impact grid, and optional data-backed building/depot/field-operation layers. Building geometry is queried only for the current bounding box at zoom 13 or higher and capped to 1,000 features per request.
- **Impact assessment** — persisted exposure totals, charts, and a searchable/sortable zone table. Selecting a row opens that zone on the map.
- **Resource inventory** — stored balances and immutable movement history. With no configured stock it shows an explicit empty state; the UI does not create or seed inventory.
- **Data sources** — ingestion status and source provenance, with catalogue discovery distinguished from satellite damage analysis.
- **System health** — API, database, PostGIS, and ingestion status based on live queries and recorded ingestion runs.

The GDACS geometry is an analyst-reported affected area, not satellite-confirmed flooding. Sentinel catalogue metadata is discovered and stored, but satellite imagery damage analysis is not implemented. Missing aggregate values are shown as unavailable rather than inferred.

## Quality checks

From `frontend/`:

```powershell
npm run lint
npm run build
```

Dashboard API endpoints are documented at `http://127.0.0.1:8000/docs` while the backend is running.
