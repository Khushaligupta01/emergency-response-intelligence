import { useCallback, useEffect, useRef, useState } from 'react'
import { BrowserRouter, NavLink, Route, Routes, useLocation, useNavigate } from 'react-router-dom'
import { dashboardApi } from './api/client'
import {
  DataSourcesPage,
  DisasterMapPage,
  ImpactAssessmentPage,
  OverviewPage,
  ResourceInventoryPage,
  SystemHealthPage,
} from './pages/DashboardPages'
import type { ApiState } from './pages/DashboardPages'
import type {
  BasicFeatureCollection,
  BuildingCollection,
  HealthData,
  ImpactData,
  Overview,
  ResourceData,
  SourceStatus,
  ZoneCollection,
} from './types/dashboard'
import './App.css'

type DataKey =
  | 'overview'
  | 'impact'
  | 'sources'
  | 'resources'
  | 'health'
  | 'zones'
  | 'affectedArea'
  | 'depots'
  | 'operations'
  | 'buildings'

type DashboardState = {
  overview: ApiState<Overview>
  impact: ApiState<ImpactData>
  sources: ApiState<{ sources: SourceStatus[] }>
  resources: ApiState<ResourceData>
  health: ApiState<HealthData>
  zones: ApiState<ZoneCollection>
  affectedArea: ApiState<BasicFeatureCollection>
  depots: ApiState<BasicFeatureCollection>
  operations: ApiState<BasicFeatureCollection>
  buildings: ApiState<BuildingCollection>
}

const emptyState = <T,>(): ApiState<T> => ({ data: null, loading: true, error: null })

const initialState: DashboardState = {
  overview: emptyState(),
  impact: emptyState(),
  sources: emptyState(),
  resources: emptyState(),
  health: emptyState(),
  zones: emptyState(),
  affectedArea: emptyState(),
  depots: emptyState(),
  operations: emptyState(),
  buildings: { data: null, loading: false, error: null },
}

const pageTitles: Record<string, { title: string; subtitle: string }> = {
  '/': { title: 'Operations overview', subtitle: 'A live view of the data currently available for response planning.' },
  '/map': { title: 'Disaster map', subtitle: 'Explore reported impact, severity grid, and spatial resources.' },
  '/impact': { title: 'Impact assessment', subtitle: 'Population and infrastructure exposure from stored assessment data.' },
  '/inventory': { title: 'Resource inventory', subtitle: 'Operational stock and audit records registered in the system.' },
  '/sources': { title: 'Data sources', subtitle: 'Source provenance and the latest recorded ingestion results.' },
  '/health': { title: 'System health', subtitle: 'Backend, database and source status based on current evidence.' },
}

function AppShell() {
  const [data, setData] = useState<DashboardState>(initialState)
  const [refreshKey, setRefreshKey] = useState(0)
  const [selectedZoneId, setSelectedZoneId] = useState<number | null>(null)
  const location = useLocation()
  const navigate = useNavigate()
  const buildingRequest = useRef<AbortController | null>(null)
  const buildingTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const title = pageTitles[location.pathname] ?? pageTitles['/']
  const retry = useCallback(() => setRefreshKey((key) => key + 1), [])

  useEffect(() => {
    const controller = new AbortController()
    const load = <K extends Exclude<DataKey, 'buildings'>>(
      key: K,
      request: (signal: AbortSignal) => Promise<DashboardState[K]['data'] & object>,
    ) => {
      setData((previous) => ({
        ...previous,
        [key]: { ...previous[key], loading: true, error: null },
      }))
      request(controller.signal)
        .then((value) => {
          if (controller.signal.aborted) return
          setData((previous) => ({
            ...previous,
            [key]: { data: value, loading: false, error: null },
          }))
        })
        .catch((error: unknown) => {
          if (controller.signal.aborted) return
          const message = error instanceof Error ? error.message : 'Unexpected API error'
          setData((previous) => ({
            ...previous,
            [key]: { ...previous[key], loading: false, error: message },
          }))
        })
    }

    load('overview', dashboardApi.overview)
    load('impact', dashboardApi.impact)
    load('sources', dashboardApi.sources)
    load('resources', dashboardApi.resources)
    load('health', dashboardApi.health)
    load('zones', dashboardApi.zones)
    load('affectedArea', dashboardApi.affectedArea)
    load('depots', dashboardApi.depots)
    load('operations', dashboardApi.operations)
    return () => controller.abort()
  }, [refreshKey])

  useEffect(() => () => {
    if (buildingTimer.current) clearTimeout(buildingTimer.current)
    buildingRequest.current?.abort()
  }, [])

  const loadBuildings = useCallback((
    bbox: { west: number; south: number; east: number; north: number },
    zoom: number,
  ) => {
    if (buildingTimer.current) clearTimeout(buildingTimer.current)
    buildingTimer.current = setTimeout(() => {
      buildingRequest.current?.abort()
      const controller = new AbortController()
      buildingRequest.current = controller
      setData((previous) => ({
        ...previous,
        buildings: { ...previous.buildings, loading: true, error: null },
      }))
      dashboardApi.buildings(bbox, zoom, controller.signal)
        .then((value) => {
          if (!controller.signal.aborted) {
            setData((previous) => ({
              ...previous,
              buildings: { data: value, loading: false, error: null },
            }))
          }
        })
        .catch((error: unknown) => {
          if (!controller.signal.aborted) {
            setData((previous) => ({
              ...previous,
              buildings: {
                ...previous.buildings,
                loading: false,
                error: error instanceof Error ? error.message : 'Unable to load building exposure.',
              },
            }))
          }
        })
    }, 250)
  }, [])

  const sharedProps = {
    ...data,
    retry,
    selectedZoneId,
    selectZone: setSelectedZoneId,
    showMap: () => navigate('/map'),
    loadBuildings,
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <NavLink to="/" className="brand">
          <span className="brand-mark">ER</span>
          <span><strong>Response</strong><small>INTELLIGENCE PLATFORM</small></span>
        </NavLink>
        <div className="nav-label">COMMAND</div>
        <nav className="primary-nav" aria-label="Main navigation">
          <NavigationLink to="/" code="01" label="Overview" />
          <NavigationLink to="/map" code="02" label="Disaster map" />
          <NavigationLink to="/impact" code="03" label="Impact assessment" />
          <NavigationLink to="/inventory" code="04" label="Resource inventory" />
          <NavigationLink to="/sources" code="05" label="Data sources" />
          <NavigationLink to="/health" code="06" label="System health" />
        </nav>
        <div className="sidebar-footer">
          <span className="sidebar-status-dot" />
          <div><strong>Week 1–2 foundation</strong><span>Data and operations layer</span></div>
        </div>
      </aside>
      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb"><span>COMMAND CENTER</span><b>/</b>{title.title.toUpperCase()}</div>
          <div className="topbar-right">
            <div className="data-state"><span className="data-state-dot" />DATABASE-LINKED VIEW</div>
            <button className="button button-refresh" onClick={retry} aria-label="Refresh dashboard data">↻ <span>Refresh</span></button>
          </div>
        </header>
        <div className="page-content">
          <div className="page-title">
            <div><h1>{title.title}</h1><p>{title.subtitle}</p></div>
            <div className="page-title-meta">
              <span>ACTIVE CASE</span>
              <strong>
                {data.overview.data
                  ? `${data.overview.data.disaster.source} · ${data.overview.data.disaster.external_id}`
                  : 'Not available'}
              </strong>
            </div>
          </div>
          <Routes>
            <Route path="/" element={<OverviewPage {...sharedProps} />} />
            <Route path="/map" element={<DisasterMapPage {...sharedProps} />} />
            <Route path="/impact" element={<ImpactAssessmentPage {...sharedProps} />} />
            <Route path="/inventory" element={<ResourceInventoryPage {...sharedProps} />} />
            <Route path="/sources" element={<DataSourcesPage {...sharedProps} />} />
            <Route path="/health" element={<SystemHealthPage {...sharedProps} />} />
            <Route path="*" element={<OverviewPage {...sharedProps} />} />
          </Routes>
          <footer className="page-footer">For situational awareness only · GDACS geometry represents a reported affected area, not confirmed inundation.</footer>
        </div>
      </main>
    </div>
  )
}

function NavigationLink({ to, code, label }: { to: string; code: string; label: string }) {
  return (
    <NavLink to={to} end={to === '/'} className={({ isActive }) => `nav-link${isActive ? ' nav-link-active' : ''}`}>
      <span className="nav-code">{code}</span><span>{label}</span><span className="nav-arrow">›</span>
    </NavLink>
  )
}

export default function App() {
  return <BrowserRouter><AppShell /></BrowserRouter>
}
