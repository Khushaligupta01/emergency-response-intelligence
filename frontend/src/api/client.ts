import type {
  BasicFeatureCollection,
  BuildingCollection,
  HealthData,
  ImpactData,
  Overview,
  ResourceData,
  SourceStatus,
  ZoneCollection,
} from '../types/dashboard'

const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? 'http://127.0.0.1:8000').replace(/\/$/, '')

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${baseUrl}${path}`, { signal })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new Error('Backend unavailable — retry', { cause: error })
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null
    throw new Error(body?.detail ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const dashboardApi = {
  overview: (signal?: AbortSignal) => get<Overview>('/api/v1/overview', signal),
  impact: (signal?: AbortSignal) => get<ImpactData>('/api/v1/impact', signal),
  zones: (signal?: AbortSignal) => get<ZoneCollection>('/api/v1/zones', signal),
  affectedArea: (signal?: AbortSignal) =>
    get<BasicFeatureCollection>('/api/v1/map/affected-area', signal),
  depots: (signal?: AbortSignal) =>
    get<BasicFeatureCollection>('/api/v1/map/depots', signal),
  operations: (signal?: AbortSignal) =>
    get<BasicFeatureCollection>('/api/v1/map/operations', signal),
  buildings: (
    bbox: { west: number; south: number; east: number; north: number },
    zoom: number,
    signal?: AbortSignal,
  ) => {
    const params = new URLSearchParams({
      west: String(bbox.west),
      south: String(bbox.south),
      east: String(bbox.east),
      north: String(bbox.north),
      zoom: String(zoom),
    })
    return get<BuildingCollection>(`/api/v1/buildings?${params}`, signal)
  },
  resources: (signal?: AbortSignal) => get<ResourceData>('/api/v1/resources', signal),
  sources: (signal?: AbortSignal) =>
    get<{ sources: SourceStatus[] }>('/api/v1/sources', signal),
  health: (signal?: AbortSignal) => get<HealthData>('/api/v1/health', signal),
}
