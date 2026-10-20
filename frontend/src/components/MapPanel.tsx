import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import { GeoJSON, MapContainer, TileLayer, useMap, useMapEvents } from 'react-leaflet'
import type { Feature, Geometry } from 'geojson'
import type {
  BasicFeatureCollection,
  BuildingCollection,
  ZoneCollection,
  ZoneProperties,
} from '../types/dashboard'

const severityColors: Record<string, string> = {
  Low: '#23a37a',
  Moderate: '#e1a93b',
  High: '#e46b3f',
  Critical: '#bb3440',
}

interface MapPanelProps {
  zones: ZoneCollection | null
  affectedArea: BasicFeatureCollection | null
  depots: BasicFeatureCollection | null
  operations: BasicFeatureCollection | null
  buildings: BuildingCollection | null
  buildingsLoading: boolean
  selectedZoneId: number | null
  onSelectZone: (zoneId: number) => void
  onViewportChange: (bbox: { west: number; south: number; east: number; north: number }, zoom: number) => void
}

function MapViewport({ onViewportChange }: Pick<MapPanelProps, 'onViewportChange'>) {
  const map = useMapEvents({
    moveend: () => {
      const bounds = map.getBounds()
      onViewportChange(
        {
          west: bounds.getWest(),
          south: bounds.getSouth(),
          east: bounds.getEast(),
          north: bounds.getNorth(),
        },
        map.getZoom(),
      )
    },
  })
  return null
}

function FitAffectedArea({ affectedArea }: { affectedArea: BasicFeatureCollection | null }) {
  const map = useMap()
  const fitted = useRef(false)
  useEffect(() => {
    if (!affectedArea?.features.length || fitted.current) return
    const bounds = L.geoJSON(affectedArea as GeoJSON.GeoJsonObject).getBounds()
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [28, 28], maxZoom: 10 })
      fitted.current = true
    }
  }, [affectedArea, map])
  return null
}

function popupElement(title: string, properties: Record<string, unknown>): HTMLElement {
  const container = document.createElement('div')
  const heading = document.createElement('strong')
  heading.textContent = title
  container.append(heading)
  for (const [label, value] of Object.entries(properties)) {
    if (value === null || value === undefined || value === '') continue
    const line = document.createElement('div')
    line.textContent = `${label.replaceAll('_', ' ')}: ${String(value)}`
    container.append(line)
  }
  return container
}

function zoneStyle(feature: Feature<Geometry, ZoneProperties> | undefined, selectedZoneId: number | null) {
  const selected = feature?.id === selectedZoneId
  const color = severityColors[feature?.properties.severity_class ?? ''] ?? '#718096'
  return {
    color: selected ? '#172c3d' : color,
    fillColor: color,
    weight: selected ? 3 : 1,
    fillOpacity: selected ? 0.66 : 0.42,
  }
}

export function MapPanel(props: MapPanelProps) {
  const [showArea, setShowArea] = useState(true)
  const [showZones, setShowZones] = useState(true)
  const [showBuildings, setShowBuildings] = useState(false)
  const [showDepots, setShowDepots] = useState(true)
  const [showOperations, setShowOperations] = useState(true)
  const [mapZoom, setMapZoom] = useState(7)
  const selected = props.zones?.features.find((zone) => zone.id === props.selectedZoneId)
  const selectedCenter = useMemo(
    () => selected ? L.geoJSON(selected as GeoJSON.GeoJsonObject).getBounds().getCenter() : null,
    [selected],
  )
  const handleViewportChange = (bbox: { west: number; south: number; east: number; north: number }, zoom: number) => {
    setMapZoom(zoom)
    props.onViewportChange(bbox, zoom)
  }

  return (
    <div className="map-shell">
      <div className="map-controls">
        <strong>Map layers</strong>
        <label><input type="checkbox" checked={showArea} onChange={(e) => setShowArea(e.target.checked)} /> GDACS reported affected area</label>
        <label><input type="checkbox" checked={showZones} onChange={(e) => setShowZones(e.target.checked)} /> Severity grid</label>
        <label><input type="checkbox" checked={showBuildings} onChange={(e) => setShowBuildings(e.target.checked)} /> Building exposure</label>
        <label><input type="checkbox" checked={showDepots} onChange={(e) => setShowDepots(e.target.checked)} /> Resource depots</label>
        <label><input type="checkbox" checked={showOperations} onChange={(e) => setShowOperations(e.target.checked)} /> Field operations</label>
        <p className="map-help">Building footprints load only at zoom 13+ and are limited to the visible map area.</p>
      </div>
      <div className="map-frame">
        <MapContainer center={[25.8, 85.5]} zoom={7} scrollWheelZoom className="leaflet-map">
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          />
          <MapViewport onViewportChange={handleViewportChange} />
          <FitAffectedArea affectedArea={props.affectedArea} />
          {selectedCenter && <FocusZone center={selectedCenter} />}
          {showArea && props.affectedArea && (
            <GeoJSON
              data={props.affectedArea as GeoJSON.GeoJsonObject}
              style={{ color: '#245fa8', weight: 3, fillColor: '#5792db', fillOpacity: 0.18, dashArray: '7 5' }}
              onEachFeature={(feature, layer) => {
                const properties = feature.properties as Record<string, unknown> | null
                layer.bindPopup(popupElement('GDACS Reported Affected Area', properties ?? {}))
              }}
            />
          )}
          {showZones && props.zones && (
            <GeoJSON
              key="zones"
              data={props.zones as GeoJSON.GeoJsonObject}
              style={(feature) => zoneStyle(feature as Feature<Geometry, ZoneProperties>, props.selectedZoneId)}
              onEachFeature={(feature, layer) => {
                const properties = feature.properties as ZoneProperties
                const fields = {
                  severity: properties.severity_class ?? 'Not available',
                  impact_score: properties.impact_score ?? 'Not available',
                  population: properties.population_count ?? 'Not available',
                  buildings: properties.building_count ?? 'Not available',
                  footprint_area_km2: properties.building_footprint_area_km2 ?? 'Not available',
                  road_length_km: properties.road_length_km ?? 'Not available',
                  hazard_coverage: properties.hazard_coverage ?? 'Not available',
                }
                layer.bindPopup(popupElement(properties.zone_code, fields))
                layer.on('click', () => {
                  if (typeof feature.id === 'number') props.onSelectZone(feature.id)
                })
              }}
            />
          )}
          {showBuildings && props.buildings?.features.length ? (
            <GeoJSON
              data={props.buildings as GeoJSON.GeoJsonObject}
              style={{ color: '#7346aa', weight: 1, fillColor: '#9b76c7', fillOpacity: 0.7 }}
              onEachFeature={(feature, layer) =>
                layer.bindPopup(popupElement('OSM building exposure', (feature.properties ?? {}) as Record<string, unknown>))
              }
            />
          ) : null}
          {showDepots && props.depots?.features.length ? (
            <GeoJSON
              data={props.depots as GeoJSON.GeoJsonObject}
              pointToLayer={(_feature, latlng) => L.circleMarker(latlng, { radius: 7, color: '#146e57', fillColor: '#37b78f', fillOpacity: 0.9 })}
              onEachFeature={(feature, layer) =>
                layer.bindPopup(popupElement('Resource depot', (feature.properties ?? {}) as Record<string, unknown>))
              }
            />
          ) : null}
          {showOperations && props.operations?.features.length ? (
            <GeoJSON
              data={props.operations as GeoJSON.GeoJsonObject}
              pointToLayer={(_feature, latlng) => L.circleMarker(latlng, { radius: 7, color: '#b4641c', fillColor: '#edaa4a', fillOpacity: 0.9 })}
              onEachFeature={(feature, layer) =>
                layer.bindPopup(popupElement('Field operation', (feature.properties ?? {}) as Record<string, unknown>))
              }
            />
          ) : null}
        </MapContainer>
        {showBuildings && props.buildingsLoading && <div className="map-notice">Loading visible building footprints…</div>}
        {showBuildings && !props.buildingsLoading && mapZoom < 13 && props.buildings && (
          <div className="map-notice">
            {new Intl.NumberFormat('en-IN').format(props.buildings.summary.count)} affected OSM footprints in the database. Zoom to level 13+ for visible footprints.
          </div>
        )}
        {showBuildings && !props.buildingsLoading && mapZoom >= 13 && props.buildings && !props.buildings.features.length && (
          <div className="map-notice">No building footprints intersect this view.</div>
        )}
        {props.buildings?.truncated && <div className="map-notice map-notice-bottom">Showing up to 1,000 footprints in this view.</div>}
      </div>
      <div className="map-legend">
        <strong>Grid severity</strong>
        {Object.entries(severityColors).map(([name, color]) => (
          <span key={name}><i style={{ backgroundColor: color }} />{name}</span>
        ))}
        <span><i className="legend-area" />GDACS reported area</span>
      </div>
    </div>
  )
}

function FocusZone({ center }: { center: L.LatLng }) {
  const map = useMap()
  useEffect(() => {
    map.flyTo(center, Math.max(map.getZoom(), 11), { duration: 0.7 })
  }, [center, map])
  return null
}
