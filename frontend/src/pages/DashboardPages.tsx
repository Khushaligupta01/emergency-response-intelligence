import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { MapPanel } from '../components/MapPanel'
import type {
  BasicFeatureCollection,
  BuildingCollection,
  HealthData,
  ImpactData,
  Overview,
  ResourceData,
  SourceStatus,
  ZoneCollection,
  ZoneProperties,
} from '../types/dashboard'

export interface ApiState<T> {
  data: T | null
  loading: boolean
  error: string | null
}

interface SharedPageProps {
  overview: ApiState<Overview>
  impact: ApiState<ImpactData>
  sources: ApiState<{ sources: SourceStatus[] }>
  resources: ApiState<ResourceData>
  health: ApiState<HealthData>
  zones: ApiState<ZoneCollection>
  affectedArea: ApiState<BasicFeatureCollection>
  depots: ApiState<BasicFeatureCollection>
  operations: ApiState<BasicFeatureCollection>
  retry: () => void
  selectedZoneId: number | null
  selectZone: (id: number) => void
  showMap?: () => void
  buildings?: ApiState<BuildingCollection>
  loadBuildings?: (
    bbox: { west: number; south: number; east: number; north: number },
    zoom: number,
  ) => void
}

const severityColors: Record<string, string> = {
  Low: '#23a37a',
  Moderate: '#e1a93b',
  High: '#e46b3f',
  Critical: '#bb3440',
  'Not classified': '#718096',
}

function formatNumber(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'Not available'
  return new Intl.NumberFormat('en-IN', { maximumFractionDigits: digits }).format(Number(value))
}

function formatDate(value: string | null | undefined): string {
  if (!value) return 'Not available'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? 'Not available'
    : new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function LoadingBlock({ label = 'Loading live data…' }: { label?: string }) {
  return <div className="loading-block"><span className="loading-dot" />{label}</div>
}

function ErrorBlock({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="error-block" role="alert">
      <span>{message === 'Backend unavailable — retry' ? message : `Could not load data: ${message}`}</span>
      <button className="button button-quiet" onClick={retry}>Retry</button>
    </div>
  )
}

function MetricCard({
  label,
  value,
  detail,
  tone = 'blue',
}: {
  label: string
  value: string
  detail?: string
  tone?: string
}) {
  return (
    <article className={`metric-card metric-${tone}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {detail && <span className="metric-detail">{detail}</span>}
    </article>
  )
}

export function OverviewPage(props: SharedPageProps) {
  const overview = props.overview.data
  const sourceList = props.sources.data?.sources ?? []
  if (props.overview.loading && !overview) return <LoadingBlock />
  if (!overview) return <ErrorBlock message={props.overview.error ?? 'No overview data returned.'} retry={props.retry} />

  const { disaster, population, buildings } = overview
  return (
    <div className="page-stack">
      {props.overview.error && <ErrorBlock message={props.overview.error} retry={props.retry} />}
      <section className="incident-banner">
        <div>
          <div className="eyebrow">CURRENT TRACKED EVENT · {disaster.source}</div>
          <h2>{disaster.name || 'Unnamed disaster'}</h2>
          <p>{disaster.disaster_type} · {disaster.country} · External ID {disaster.external_id}</p>
        </div>
        <div className="incident-tags">
          <span className="tag tag-blue">{disaster.severity ?? 'Severity not available'}</span>
          <span className="tag tag-outline">
            {overview.affected_area?.episode_id
              ? `Episode ${overview.affected_area.episode_id} · GDACS reported area`
              : 'GDACS reported area'}
          </span>
        </div>
      </section>
      <div className="metric-grid">
        <MetricCard label="Affected population" value={formatNumber(population.affected_population)} detail="WorldPop exposure estimate" tone="red" />
        <MetricCard label="Affected buildings" value={formatNumber(buildings.affected_buildings)} detail="OSM footprints in reported area" tone="orange" />
        <MetricCard label="Impact grid zones" value={formatNumber(overview.zone_count)} detail="Persisted in PostGIS" tone="blue" />
        <MetricCard label="Critical zones" value={formatNumber(overview.critical_zones)} detail={`${formatNumber(overview.high_zones)} high severity`} tone="purple" />
      </div>
      <div className="content-grid overview-grid">
        <section className="panel panel-chart">
          <div className="panel-heading">
            <div><div className="eyebrow">ZONE CLASSIFICATION</div><h3>Severity distribution</h3></div>
            <span className="small-muted">{formatNumber(overview.zone_count)} zones</span>
          </div>
          <SeverityChart data={overview.severity_distribution} />
        </section>
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">EVENT DETAILS</div><h3>Situation summary</h3></div></div>
          <dl className="detail-list">
            <Detail label="Event type" value={disaster.disaster_type} />
            <Detail label="Event period" value={`${formatDate(disaster.start_time)} — ${formatDate(disaster.end_time)}`} />
            <Detail label="Reported area" value={overview.affected_area?.area_km2 == null ? 'Not available' : `${formatNumber(overview.affected_area.area_km2, 1)} km²`} />
            <Detail label="Affected population" value={formatNumber(population.affected_population)} />
            <Detail label="Data updated" value={formatDate(overview.last_data_update)} />
          </dl>
          <Link className="text-link" to="/map">Open disaster map <span aria-hidden="true">→</span></Link>
        </section>
      </div>
      <div className="content-grid">
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">INGESTION SOURCES</div><h3>Latest source status</h3></div><Link to="/sources" className="text-link">All sources →</Link></div>
          {props.sources.loading && !props.sources.data ? <LoadingBlock /> : props.sources.error ? <ErrorBlock message={props.sources.error} retry={props.retry} /> : (
            <div className="source-list compact">
              {sourceList.slice(0, 5).map((source) => (
                <SourceRow key={source.name} source={source} />
              ))}
            </div>
          )}
        </section>
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">LATEST ACTIVITY</div><h3>Recent ingestion runs</h3></div><span className="small-muted">Live database records</span></div>
          {overview.recent_ingestion.length ? (
            <div className="run-list">
              {overview.recent_ingestion.slice(0, 5).map((run) => (
                <div className="run-row" key={run.id}>
                  <StatusPill value={run.status} />
                  <strong>{run.source_name}</strong>
                  <span>{formatNumber(run.items_persisted)} saved</span>
                  <time>{formatDate(run.started_at)}</time>
                </div>
              ))}
            </div>
          ) : <EmptyState text="No ingestion runs have been recorded." />}
        </section>
      </div>
    </div>
  )
}

export function DisasterMapPage(props: SharedPageProps) {
  if (props.zones.loading && !props.zones.data) return <LoadingBlock label="Loading map layers from PostGIS…" />
  if (props.zones.error) return <ErrorBlock message={props.zones.error} retry={props.retry} />
  return (
    <div className="page-stack">
      <div className="map-caption"><span className="map-caption-mark" /> GDACS reported affected area — not satellite-confirmed flooding.</div>
      <MapPanel
        zones={props.zones.data}
        affectedArea={props.affectedArea.data}
        depots={props.depots.data}
        operations={props.operations.data}
        buildings={props.buildings?.data ?? null}
        buildingsLoading={props.buildings?.loading ?? false}
        selectedZoneId={props.selectedZoneId}
        onSelectZone={props.selectZone}
        onViewportChange={props.loadBuildings ?? (() => undefined)}
      />
      {props.affectedArea.error && <ErrorBlock message={props.affectedArea.error} retry={props.retry} />}
      {props.buildings?.error && <ErrorBlock message={props.buildings.error} retry={props.retry} />}
      {props.depots.error && <ErrorBlock message={props.depots.error} retry={props.retry} />}
      {props.operations.error && <ErrorBlock message={props.operations.error} retry={props.retry} />}
      {!props.affectedArea.loading && !props.affectedArea.error && !props.affectedArea.data?.features.length && (
        <EmptyState text="No spatial geometry available." />
      )}
    </div>
  )
}

export function ImpactAssessmentPage(props: SharedPageProps) {
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<keyof ZoneProperties>('impact_score')
  const [sortDescending, setSortDescending] = useState(true)
  const impact = props.impact.data
  const rows = useMemo(() => {
    if (!impact) return []
    return impact.zones
      .filter((zone) => zone.zone_code.toLowerCase().includes(search.toLowerCase()))
      .sort((a, b) => {
        const av = a[sortKey]
        const bv = b[sortKey]
        const order = typeof av === 'string' && typeof bv === 'string'
          ? av.localeCompare(bv)
          : Number(av ?? -Infinity) - Number(bv ?? -Infinity)
        return sortDescending ? -order : order
      })
  }, [impact, search, sortDescending, sortKey])

  if (props.impact.loading && !impact) return <LoadingBlock />
  if (!impact) return <ErrorBlock message={props.impact.error ?? 'No impact data returned.'} retry={props.retry} />

  const chooseSort = (key: keyof ZoneProperties) => {
    if (key === sortKey) setSortDescending(!sortDescending)
    else {
      setSortKey(key)
      setSortDescending(true)
    }
  }
  return (
    <div className="page-stack">
      {props.impact.error && <ErrorBlock message={props.impact.error} retry={props.retry} />}
      <div className="metric-grid metric-grid-six">
        <MetricCard label="Total population" value={formatNumber(impact.population.total_population)} tone="blue" />
        <MetricCard label="Affected population" value={formatNumber(impact.population.affected_population)} tone="red" />
        <MetricCard label="Valid population pixels" value={formatNumber(impact.population.valid_population_pixels)} tone="purple" />
        <MetricCard label="Affected population pixels" value={formatNumber(impact.population.affected_population_pixels)} tone="orange" />
        <MetricCard label="Affected buildings" value={formatNumber(impact.buildings.affected_buildings)} tone="green" />
        <MetricCard label="Total OSM buildings" value="Not available" detail="Not stored as an all-area total" tone="slate" />
        <MetricCard label="Total footprint area" value="Not available" detail="Not stored as an all-area total" tone="slate" />
        <MetricCard label="Affected footprint area" value={`${formatNumber(impact.buildings.affected_footprint_area_km2 * 1_000_000)} m²`} detail="OSM footprint portions in the reported area" tone="orange" />
      </div>
      <div className="content-grid impact-charts">
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">CLASSIFICATION</div><h3>Severity distribution</h3></div></div>
          <SeverityChart data={impact.severity_distribution} />
        </section>
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">WORLDPOP EXPOSURE</div><h3>Population exposure</h3></div></div>
          <ResponsiveContainer width="100%" height={245}>
            <BarChart data={[
              { name: 'Population', total: impact.population.total_population, affected: impact.population.affected_population },
              { name: 'Pixels', total: impact.population.valid_population_pixels, affected: impact.population.affected_population_pixels },
            ]}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="name" />
              <YAxis tickFormatter={(value: number) => formatNumber(value)} />
              <Tooltip formatter={(value) => formatNumber(Number(value))} />
              <Legend />
              <Bar dataKey="total" name="Total" fill="#aebdca" radius={[5, 5, 0, 0]} />
              <Bar dataKey="affected" name="Affected" fill="#d85b4b" radius={[5, 5, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </section>
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">OSM EXPOSURE</div><h3>Building exposure</h3></div></div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={impact.severity_distribution.map((item) => ({
              severity: item.severity_class,
              buildings: impact.zones
                .filter((zone) => (zone.severity_class ?? 'Not classified') === item.severity_class)
                .reduce((sum, zone) => sum + Number(zone.building_count ?? 0), 0),
            }))}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="severity" />
              <YAxis tickFormatter={(value: number) => formatNumber(value)} />
              <Tooltip formatter={(value) => formatNumber(Number(value))} />
              <Bar dataKey="buildings" name="Affected buildings in grid zones" fill="#806bb1" radius={[5, 5, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
          <p className="chart-footnote">The bars aggregate zone exposure values. Total all-area building count and total footprint area are not stored; the affected footprint area is shown above.</p>
        </section>
        <section className="panel">
          <div className="panel-heading"><div><div className="eyebrow">ZONE SCORE</div><h3>Impact score by severity</h3></div></div>
          <ResponsiveContainer width="100%" height={245}>
            <BarChart data={impact.severity_distribution.map((item) => {
              const values = impact.zones
                .filter((zone) => (zone.severity_class ?? 'Not classified') === item.severity_class)
                .map((zone) => Number(zone.impact_score))
                .filter(Number.isFinite)
              return {
                severity: item.severity_class,
                average: values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null,
              }
            })}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="severity" />
              <YAxis domain={[0, 1]} />
              <Tooltip formatter={(value) => Number(value).toFixed(3)} />
              <Bar dataKey="average" name="Average impact score" radius={[5, 5, 0, 0]}>
                {impact.severity_distribution.map((item) => <Cell key={item.severity_class} fill={severityColors[item.severity_class] ?? '#718096'} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </section>
      </div>
      <section className="panel table-panel">
        <div className="panel-heading">
          <div><div className="eyebrow">POSTGIS GRID</div><h3>Zone assessment</h3></div>
          <label className="table-search"><span>Search zones</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Enter a zone ID…" /></label>
        </div>
        <div className="table-scroll">
          <table>
            <thead><tr>
              <SortableHeader label="Zone" column="zone_code" current={sortKey} descending={sortDescending} onSort={chooseSort} />
              <SortableHeader label="Population" column="population_count" current={sortKey} descending={sortDescending} onSort={chooseSort} />
              <SortableHeader label="Buildings" column="building_count" current={sortKey} descending={sortDescending} onSort={chooseSort} />
              <SortableHeader label="Footprint km²" column="building_footprint_area_km2" current={sortKey} descending={sortDescending} onSort={chooseSort} />
              <SortableHeader label="Impact score" column="impact_score" current={sortKey} descending={sortDescending} onSort={chooseSort} />
              <SortableHeader label="Severity" column="severity_class" current={sortKey} descending={sortDescending} onSort={chooseSort} />
            </tr></thead>
            <tbody>{rows.slice(0, 250).map((zone) => (
              <tr
                key={zone.id}
                className={props.selectedZoneId === zone.id ? 'selected-row' : ''}
                onClick={() => {
                  props.selectZone(zone.id)
                  props.showMap?.()
                }}
                title="Select this zone on the map"
              >
                <td className="zone-id">{zone.zone_code}</td>
                <td>{formatNumber(zone.population_count)}</td>
                <td>{formatNumber(zone.building_count)}</td>
                <td>{formatNumber(zone.building_footprint_area_km2, 6)}</td>
                <td>{formatNumber(zone.impact_score, 4)}</td>
                <td><SeverityTag value={zone.severity_class} /></td>
              </tr>
            ))}</tbody>
          </table>
        </div>
        <div className="table-foot">Showing {formatNumber(Math.min(rows.length, 250))} of {formatNumber(rows.length)} matching zones. Select a row to focus it on the map.</div>
      </section>
    </div>
  )
}

export function ResourceInventoryPage(props: SharedPageProps) {
  const data = props.resources.data
  if (props.resources.loading && !data) return <LoadingBlock />
  if (!data) return <ErrorBlock message={props.resources.error ?? 'No resource data returned.'} retry={props.retry} />
  const hasStock = data.balances.some((balance) => Number(balance.quantity) > 0)
  return (
    <div className="page-stack">
      {props.resources.error && <ErrorBlock message={props.resources.error} retry={props.retry} />}
      {!hasStock && <div className="empty-banner"><strong>No operational inventory currently registered.</strong><span>Resource types, depots and stock are not seeded with sample quantities.</span></div>}
      <div className="metric-grid">
        <MetricCard label="Resource types" value={formatNumber(data.resource_types.length)} tone="blue" />
        <MetricCard label="Registered depots" value={formatNumber(data.depots.length)} tone="green" />
        <MetricCard label="Stock balance rows" value={formatNumber(data.balances.length)} tone="orange" />
        <MetricCard label="Recorded movements" value={formatNumber(data.transactions.length)} tone="purple" />
      </div>
      <section className="panel table-panel">
        <div className="panel-heading"><div><div className="eyebrow">ON-HAND STOCK</div><h3>Inventory balances</h3></div></div>
        {data.balances.length ? <div className="table-scroll"><table><thead><tr><th>Resource</th><th>Category</th><th>Depot</th><th>On hand</th><th>Reserved</th><th>Available</th><th>Updated</th></tr></thead><tbody>
          {data.balances.map((item) => <tr key={`${item.resource_type_id}-${item.depot_id}`}><td>{item.resource_type}</td><td>{item.category}</td><td>{item.depot}</td><td>{formatNumber(item.quantity, 2)} {item.unit}</td><td>{formatNumber(item.reserved_quantity, 2)} {item.unit}</td><td>{formatNumber(item.available_quantity, 2)} {item.unit}</td><td>{formatDate(item.updated_at)}</td></tr>)}
        </tbody></table></div> : <EmptyState text="No operational inventory registered." />}
      </section>
      <section className="panel table-panel">
        <div className="panel-heading"><div><div className="eyebrow">AUDIT LEDGER</div><h3>Recent inventory transactions</h3></div></div>
        {data.transactions.length ? <div className="table-scroll"><table><thead><tr><th>Type</th><th>Resource</th><th>From depot</th><th>Destination</th><th>Quantity</th><th>Reference</th><th>Date</th></tr></thead><tbody>
          {data.transactions.map((item) => <tr key={item.id}><td><StatusPill value={item.transaction_type} /></td><td>{item.resource_type}</td><td>{item.depot}</td><td>{item.destination_depot ?? '—'}</td><td>{formatNumber(item.quantity, 2)} {item.unit}</td><td>{item.reference ?? '—'}</td><td>{formatDate(item.occurred_at)}</td></tr>)}
        </tbody></table></div> : <EmptyState text="No inventory movements have been recorded." />}
      </section>
      <section className="panel">
        <div className="panel-heading"><div><div className="eyebrow">RESOURCE CATALOGUE</div><h3>Configured resource types</h3></div></div>
        {data.resource_types.length ? <div className="resource-catalogue">{data.resource_types.map((item) => <div className="catalogue-item" key={item.id}><strong>{item.name}</strong><span>{item.category} · {item.unit}</span></div>)}</div> : <EmptyState text="No resource types have been configured. The backend currently provides inventory storage and audit services, but no inventory management API." />}
      </section>
    </div>
  )
}

export function DataSourcesPage(props: SharedPageProps) {
  const sources = props.sources.data?.sources
  if (props.sources.loading && !sources) return <LoadingBlock />
  if (!sources) return <ErrorBlock message={props.sources.error ?? 'No source data returned.'} retry={props.retry} />
  return (
    <div className="page-stack">
      {props.sources.error && <ErrorBlock message={props.sources.error} retry={props.retry} />}
      {(() => {
        const copernicus = sources.find((source) => source.id === 'COPERNICUS')
        const discovery = copernicus?.latest?.status === 'success' || copernicus?.latest?.status === 'not_modified'
          ? 'Connected'
          : copernicus?.latest?.status
            ? 'Degraded'
            : copernicus?.record_count
              ? 'Catalogued'
              : 'Unknown'
        return <div className="notice-panel"><strong>Satellite imagery discovery: {discovery}</strong><span>Satellite imagery is catalogued as metadata only. Satellite damage analysis is not yet implemented.</span></div>
      })()}
      <div className="source-cards">{sources.map((source) => <article className="panel source-card" key={source.name}>
        <div className="source-card-top"><div><span className="eyebrow">DATA PROVIDER</span><h3>{source.name}</h3></div><StatusPill value={source.latest?.status ?? (source.record_count ? 'available' : 'unknown')} /></div>
        <p>{source.description}</p>
        <div className="source-count">{formatNumber(source.record_count)} <span>provenance records</span></div>
        {source.latest ? <>
          <Detail label="Latest poll" value={formatDate(source.latest.started_at)} />
          <Detail label="Last status" value={source.latest.status} />
          <Detail label="Items persisted" value={formatNumber(source.latest.items_persisted)} />
          {source.latest.error_message && <div className="source-error">{source.latest.error_message}</div>}
        </> : <div className="source-footnote">{source.record_count ? 'Loaded dataset provenance; no poll history is recorded.' : 'No source data has been recorded.'}</div>}
      </article>)}</div>
    </div>
  )
}

export function SystemHealthPage(props: SharedPageProps) {
  const health = props.health.data
  if (props.health.loading && !health) return <LoadingBlock />
  if (!health) return <ErrorBlock message={props.health.error ?? 'Health status unavailable.'} retry={props.retry} />
  return (
    <div className="page-stack">
      {props.health.error && <ErrorBlock message={props.health.error} retry={props.retry} />}
      <div className={`health-banner ${health.status === 'healthy' ? 'health-good' : 'health-warn'}`}>
        <span className="health-live-dot" />System status: {health.status === 'healthy' ? 'Operational' : 'Degraded'}
      </div>
      <div className="health-grid">{health.checks.map((check) => (
        <article className="panel health-card" key={check.name}>
          <div><span className="eyebrow">COMPONENT</span><h3>{check.name}</h3></div>
          <StatusPill value={check.status} />
          <p>{check.detail}</p>
        </article>
      ))}</div>
      <div className="notice-panel"><strong>Source connectivity is based on ingestion history.</strong><span>A recent successful or not-modified poll is evidence of connectivity. Unknown means no ingestion evidence is available.</span></div>
    </div>
  )
}

function SeverityChart({ data }: { data: Overview['severity_distribution'] }) {
  return (
    <ResponsiveContainer width="100%" height={245}>
      <PieChart>
        <Pie data={data} dataKey="count" nameKey="severity_class" cx="50%" cy="50%" innerRadius={60} outerRadius={92} paddingAngle={2}>
          {data.map((entry) => <Cell key={entry.severity_class} fill={severityColors[entry.severity_class] ?? '#718096'} />)}
        </Pie>
        <Tooltip formatter={(value) => formatNumber(Number(value))} />
        <Legend verticalAlign="bottom" height={34} />
      </PieChart>
    </ResponsiveContainer>
  )
}

function Detail({ label, value }: { label: string; value: string }) {
  return <div className="detail-row"><dt>{label}</dt><dd>{value}</dd></div>
}

function SourceRow({ source }: { source: SourceStatus }) {
  const state = source.latest?.status ?? (source.record_count ? 'available' : 'unknown')
  return <div className="source-row"><div><strong>{source.name}</strong><span>{formatNumber(source.record_count)} source records</span></div><StatusPill value={state} /></div>
}

function StatusPill({ value }: { value: string }) {
  const normalized = value.toLowerCase().replaceAll('_', '-')
  const tone = ['success', 'not-modified', 'connected', 'available', 'healthy'].includes(normalized)
    ? 'status-good'
    : ['partial', 'degraded', 'running'].includes(normalized)
      ? 'status-warn'
      : ['failed', 'unavailable'].includes(normalized)
        ? 'status-bad'
        : 'status-neutral'
  return <span className={`status-pill ${tone}`}>{value.replaceAll('_', ' ')}</span>
}

function SeverityTag({ value }: { value: string | null }) {
  const normalized = value ?? 'Not classified'
  return <span className="severity-tag" style={{ color: severityColors[normalized] ?? severityColors['Not classified'] }}>{normalized}</span>
}

function SortableHeader({
  label,
  column,
  current,
  descending,
  onSort,
}: {
  label: string
  column: keyof ZoneProperties
  current: keyof ZoneProperties
  descending: boolean
  onSort: (column: keyof ZoneProperties) => void
}) {
  return <th><button className="sort-button" onClick={() => onSort(column)}>{label}{current === column ? (descending ? ' ↓' : ' ↑') : ''}</button></th>
}

function EmptyState({ text }: { text: string }) {
  return <div className="empty-state"><span className="empty-state-icon">i</span><p>{text}</p></div>
}
