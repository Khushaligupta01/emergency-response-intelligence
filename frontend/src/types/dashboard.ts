import type { Feature, FeatureCollection, Geometry } from 'geojson'

export interface Disaster {
  id: number
  external_id: string
  source: string
  disaster_type: string
  name: string
  country: string
  severity: string | null
  start_time: string | null
  end_time: string | null
  updated_at: string | null
}

export interface Overview {
  disaster: Disaster
  affected_area: {
    external_id: string
    hazard_type: string
    episode_id: string | null
    area_km2: number | null
  } | null
  population: {
    total_population: number
    affected_population: number
    valid_population_pixels: number
    affected_population_pixels: number
  }
  buildings: {
    affected_buildings: number
    affected_footprint_area_km2: number
  }
  zone_count: number
  severity_distribution: Array<{ severity_class: string; count: number }>
  critical_zones: number
  high_zones: number
  last_data_update: string | null
  recent_ingestion: IngestionRun[]
}

export interface IngestionRun {
  id: number
  source_name: string
  status: string
  started_at: string
  completed_at: string | null
  items_seen: number
  items_persisted: number
  items_failed: number
  error_message: string | null
}

export interface ZoneProperties {
  zone_code: string
  severity_class: string | null
  impact_score: number | null
  population_count: number | null
  building_count: number | null
  building_footprint_area_km2: number | null
  road_length_km: number | null
  hazard_coverage: number | null
  population_exposure: number | null
  building_exposure: number | null
  infrastructure_exposure: number | null
  vulnerability_score: number | null
}

export type ZoneFeature = Feature<Geometry, ZoneProperties>
export type ZoneCollection = FeatureCollection<Geometry, ZoneProperties>
export type BasicFeatureCollection = FeatureCollection<Geometry, Record<string, unknown>>

export interface ImpactData {
  population: Overview['population']
  buildings: Overview['buildings']
  zone_count: number
  severity_distribution: Overview['severity_distribution']
  valid_dataset_pixels: number
  zones: Array<ZoneProperties & { id: number }>
}

export interface SourceStatus {
  id: string
  name: string
  description: string
  record_count: number
  latest: {
    status: string
    started_at: string
    completed_at: string | null
    items_seen: number
    items_persisted: number
    items_failed: number
    error_message: string | null
  } | null
}

export interface ResourceData {
  balances: Array<{
    resource_type_id: number
    code: string
    resource_type: string
    category: string
    unit: string
    depot_id: number
    depot: string
    quantity: number
    reserved_quantity: number
    available_quantity: number
    updated_at: string
  }>
  resource_types: Array<{
    id: number
    code: string
    name: string
    category: string
    unit: string
    is_active: boolean
  }>
  depots: BasicFeatureCollection['features']
  transactions: Array<{
    id: number
    transaction_type: string
    quantity: number
    source: string | null
    reference: string | null
    notes: string | null
    created_by: string | null
    occurred_at: string
    resource_type: string
    unit: string
    depot: string
    destination_depot: string | null
  }>
}

export interface HealthCheck {
  name: string
  status: 'Connected' | 'Degraded' | 'Unavailable' | 'Unknown'
  detail: string
}

export interface HealthData {
  status: string
  checks: HealthCheck[]
}

export interface BuildingCollection extends BasicFeatureCollection {
  summary: {
    count: number
    area_km2: number
  }
  truncated: boolean
}
