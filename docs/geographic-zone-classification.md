# Geographic Zone Classification

## Study area and source data

The grid uses the existing GDACS event `1104121`, episode `20`, `Poly_Affected` Bihar clip and the already validated Step 2D affected OSM buildings and WorldPop population raster. The GDACS geometry is an analyst-reported affected area; it is not an observed satellite-derived inundation or proof of damage.

## Grid generation

The default regular grid is 10,000 m by 10,000 m in EPSG:32645 (WGS 84 / UTM zone 45N). Its origin is aligned to integer multiples of the configured cell size in that projected CRS. Each square is intersected with the reported affected-area geometry and only non-empty, positive-area intersections are retained. Final GeoJSON geometries are transformed to EPSG:4326.

WorldPop values are summed as estimated people per pixel, never converted from an assumed density. Each valid source pixel whose center falls in the affected area is assigned once to the containing projected grid square. Each unique Step 2D affected OSM building is assigned once by an interior point; its clipped footprint area is split across intersecting projected grid squares.

## Available features and baseline score

Each zone records the reported-area coverage fraction, estimated affected population, valid population-pixel count, unique affected building count, affected building footprint area, normalized population/building exposures, impact score, and severity class. Infrastructure exposure and vulnerability are null because no approved infrastructure or vulnerability dataset is available for this study area. No values are imputed for those missing features.

Population and building exposures are normalized by their maximum observed per-cell values. Reported-area exposure is the projected intersection area divided by the full square-cell area. The default engineering baseline is:

```text
score = (
    0.40 * reported_area_coverage
  + 0.25 * normalized_population
  + 0.15 * normalized_building_count
) / (0.40 + 0.25 + 0.15)
```

Unavailable infrastructure and vulnerability features are excluded and available weights are renormalized to sum to one. Weights are configurable through the generator's CLI flags. They are an engineering baseline, not scientifically validated domain weights or a damage model.

Default configurable severity thresholds:

| Class | Impact score |
| --- | --- |
| Low | `< 0.25` |
| Moderate | `>= 0.25` and `< 0.50` |
| High | `>= 0.50` and `< 0.75` |
| Critical | `>= 0.75` |

The cell size, weights, and thresholds can be changed with `generate_disaster_grid.py` options; the selected values are recorded in generation metadata for reproducibility.

## Outputs and database

- `data/processed/zones/disaster_grid.geojson`: EPSG:4326 FeatureCollection with zone identifiers, disaster reference, geometries, raw/normalized metrics, score, and class.
- `data/processed/zones/zone_severity_summary.json`: zone totals, score range, and class counts.
- `data/processed/zones/zone_generation_metadata.json`: inputs, projection, grid settings, feature definitions, normalization, weights, thresholds, and limitations.
- PostgreSQL/PostGIS `impact_zones`: projected-grid attributes and EPSG:4326 cell geometry linked through the real GDACS `disasters` record. Generated `GDACS-1104121-GRID-*` zones are replaced idempotently when regenerating this event's grid.

With the backend virtual environment and repository requirements installed, run from the repository root:

```powershell
.\backend\.venv\Scripts\python.exe -m pip install -r .\scripts\processing\requirements-step3.txt
.\backend\.venv\Scripts\python.exe .\scripts\processing\generate_disaster_grid.py
.\backend\.venv\Scripts\python.exe .\scripts\processing\validate_disaster_grid.py
```

## Limitations

- The score does not assert actual flooding, observed inundation, structural damage, or a validated severity outcome.
- OSM building data may omit buildings; WorldPop is a modelled population estimate and its NoData cells are excluded.
- Infrastructure and vulnerability features are unavailable and excluded.
- Grid results depend on the configured cell size, normalization maxima, score weights, and classification thresholds.
