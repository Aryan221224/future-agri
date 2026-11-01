// Typed client for the console API (/api). Errors carry the server's message.

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const r = await fetch(path, {
    method,
    headers: body === undefined || isForm ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
  })
  if (!r.ok) {
    let msg = r.statusText || `HTTP ${r.status}`
    try {
      const j = await r.json()
      if (typeof j.detail === 'string') msg = j.detail
      else if (Array.isArray(j.detail)) msg = j.detail.map((d: { loc?: string[]; msg: string }) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ')
    } catch {
      /* not JSON */
    }
    throw new ApiError(r.status, msg)
  }
  if (r.status === 204) return undefined as T
  return (await r.json()) as T
}

export const api = {
  get: <T>(p: string) => req<T>('GET', p),
  post: <T>(p: string, b: unknown = {}) => req<T>('POST', p, b),
  patch: <T>(p: string, b: unknown) => req<T>('PATCH', p, b),
  del: <T>(p: string) => req<T>('DELETE', p),
}

// ------------------------------------------------------------------ types
export type Mode = 'sim' | 'field'

export interface Template {
  id: string; name: string; crop: string; variety: string; region: string; irrigated: boolean; weeds: boolean
  diseases: string[]; field_ha: [number, number]; sowing: string; harvest: string; supported: boolean
  unsupported_reason: string; drone_use: string; fidelity_note: string
  locations: { name: string; lat: number; lon: number; country: string }[]
}
export interface Region {
  id: string; name: string; currency: string; verified: boolean; prices_assumed: boolean; area_unit: string
  aerial_default_allowed: boolean; wind_max_ms: number; buffer_m: number; max_height_m: number; records: string[]
  warnings: string[]
}
export interface Meta { version: string; templates: Template[]; regions: Region[]; job_kinds: string[]; drone: string; lan_exposed: boolean }
export interface Settings { area_unit: 'ha' | 'acre' | 'mu'; language: Lang; theme: 'auto' | 'light' | 'dark'; operator: string; default_region: string; aircraft_id: string }
export type Lang = 'en' | 'hi' | 'es' | 'pt'

export interface Farm {
  id: string; name: string; mode: Mode; template: string; crop: string; region: string; country: string | null
  year: number; lat: number; lon: number; area_ha: number | null; cell_m: number; polygon: GeoJSON | null
  sowing: string; harvest: string; status: 'new' | 'queued' | 'building' | 'ready' | 'error'
  meta: { warnings?: string[]; error?: string | null; provenance?: Provenance[]; summary?: Record<string, unknown>; seed?: number }
  created_at: string; updated_at: string
}
export interface Provenance { source: string; url: string; licence: string; retrieved: string; attribution: string; commercial_use: boolean; note: string }
export type GeoJSON = { type: string; coordinates?: unknown; features?: unknown[]; geometry?: unknown; properties?: Record<string, unknown> }

export interface Job {
  id: string; farm_id: string | null; kind: string; status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  progress: number; message: string; params: Record<string, unknown>; result: Record<string, unknown> | null
  error: string | null; worker: string | null; target: string; created_at: string; started_at: string | null; finished_at: string | null
}

export interface Scene {
  origin: { lat: number; lon: number; crs: string }
  polygon: [number, number][]
  buffer: { m: number; inner: [number, number][] | null }
  bounds: [number, number, number, number]
  terrain: { nx: number; ny: number; x0: number; y0: number; dx: number; dy: number; heights_b64: string; min: number; max: number }
  cells: { n: number; cell_m: number; x_b64: string; y_b64: string; ground_b64: string }
  layers: Record<string, string>
  layer_meta: Record<string, string>
  crop: { name: string; variety: string; sowing: string; harvest: string }
  imagery: { available: boolean; url?: string; date?: string; cloud?: number; bounds?: [number, number, number, number]; reason?: string }
  area_ha: number
  warnings: string[]
}

export interface Op {
  id: string; product: 'herbicide' | 'fungicide' | 'scan'; due: string; status: 'proposed' | 'approved' | 'flown' | 'dropped' | 'rejected'
  created: string; reason: string; targets_frac: number; settings: Record<string, unknown> | null; result: Record<string, number | string> | null
}
export interface SessionEvent { day: string; kind: string; text: string; op?: string; [k: string]: unknown }
export interface SessionState {
  day: string; phase: 'morning' | 'flights'; done: boolean; policy: string; approvals: string; das: number
  sowing: string; harvest: string; ops: Op[]; costs: Record<string, number>; missions: number; litres: number
  scans: number; fungicide_sprays: number; herbicide: string; notes: string[]; n_events: number; n_trace: number
  flight: FlightSettings; scan_das: number; scan_gsd_m: number; recent_events: SessionEvent[]; mode: Mode
  world: { has_truth: boolean; last_observed: string; waiting: string | null }
  plan: { id: string | null; name?: string | null; policy: string; flight: FlightSettings; scan_das: number; scan_gsd_m: number }
  twin: { lai: number | null; dvs: number | null; twso: number | null; herbicide_option: string; scans: number } | null
}
export interface TraceRow {
  day: string; das: number; tmax: number; tmin: number; rain_mm: number; wind_ms: number
  truth_lai: number | null; truth_cc: number | null; truth_dvs: number | null; truth_weeds_m2: number | null; truth_blight: number | null
  twin_lai: number | null; twin_dvs: number | null; twin_twso: number | null
}

export interface FlightSettings {
  height_m: number; speed_ms: number; swath_m: number; rate_l_ha: number; rtk: boolean; pump_lpm: number
  route_angle_deg: number | null; ferry_speed_ms: number; n_battery_sets: number; chargers: number
  refill_s: number; battery_swap_s: number; battery_cycles: number
}
export interface Frame { t: number; x: number; y: number; z: number; soc: number; v_cell: number; liquid_l: number; spraying: boolean; flow_lpm: number; phase: string; sortie: number }
export interface MissionResult {
  feasible: boolean; reasons: string[]; warnings: string[]; kpis: Record<string, number>; track: Frame[]
  route: { start: [number, number]; end: [number, number] }[]; events: Record<string, unknown>[]; dose_b64: string
  home: [number, number] | null; settings: FlightSettings; weather: Weather; targets_frac: number
}
export interface Weather { wind_ms: number; wind_dir_deg: number; gust_ms: number; temp_c: number }
export interface DroneSpec {
  name: string; controller: string; wheelbase_mm: number; unfolded_mm: number[]; folded_mm: number[]; rotor_diameter_m: number
  n_rotors: number; tank_l: number; n_nozzles: number; motors: { x: number; y: number }[]; nozzles: { x: number; y: number }[]
  mass_kg: Record<string, number>; hover_points: number[][]
  battery: { cells: number; capacity_ah: number; nominal_wh: number; mass_kg: number; usable_frac: number; inferred: boolean; note: string }
  pump_lpm: number[]; swath_m: number[]; swath_table_m: number; charge_min: number; link_range_m: number; temp_range_c: number[]
  limits: Record<string, number>; rtk_optional: boolean; sdk: boolean; claimed_acres_per_hour: number[]; notes: string[]
}

export interface PlanOperation {
  product: string; count_mean: number; das_first: number; das_median: number; das_last: number; date_first: string; date_last: string
  time_min?: number; crew_min?: number; sorties?: number; battery_swaps?: number; refills?: number; litres?: number; area_ha?: number; cv_pct?: number; gsd_m?: number; note?: string
}
export interface PlanPayload {
  plan: string; policy: string; flight: FlightSettings; scan: { das: number; gsd_m: number; date: string }
  operations: PlanOperation[]; totals: { missions: number; drone_h: number; crew_h: number; litres: number; scans: number }
  logistics: { battery_sets: number; chargers: number; time_min: number; ha_per_hour: number; battery_swaps: number }[]
  logistics_pick: { battery_sets: number; chargers: number; ha_per_hour: number; why: string } | null
  net_mean: number; cvar20: number; score: number; yield_dry_mean: number; currency: string; value_at_risk_ha: number | null
  n_seasons: number; season: { sowing: string; harvest: string }; rehearsal: { year: number; sowing: string; harvest: string; kind: string }
  warnings: string[]; assumptions: string[]
}
export interface Plan { id: string; farm_id: string; status: 'draft' | 'approved' | 'superseded'; source_job: string | null; payload: PlanPayload; created_at: string; approved_at: string | null }
export interface RankedPlan {
  plan: string; policy: string; flight: FlightSettings; scan_gsd_m: number; scan_das: number; net_mean: number; net_cvar20: number; score: number
  yield_dry_mean: number; fungicide_sprays_mean: number; litres_mean: number; sorties_mean: number; drone_time_min_mean: number
  off_target_l_mean: number; cv_pct_mean: number; skipped_frac_mean: number; scans_mean: number; n_seasons: number
}
export interface PerfectResult {
  summary: { best: string; net_mean: number; currency: string }; best: RankedPlan & { jiyi_settings: FlightSettings }
  ranked: RankedPlan[]; flight_ranking: { scale: string; settings: FlightSettings; score: number; feasible_frac: number; kpis_mean: Record<string, number>; breakdown: Record<string, number> }[]
  logistics: PlanPayload['logistics']; value_at_risk_ha: number; currency: string; warnings: string[]; timings_s: Record<string, number>
  rehearsal: { year: number; sowing: string; harvest: string; kind: string }
}

export interface Alert { level: 'info' | 'warn' | 'alert'; code: string; text: string; params: Record<string, unknown> }
export interface Live<T> { available: boolean; stale: boolean; stale_reason?: string; reason?: string; fetched?: string; data?: T }
export interface Outlook { available: boolean; stale?: boolean; reason?: string; source: string; issue: string; days: { day: string; p_flyable: number; wind_dawn_p50: number; rain_p50: number; tmax_p50: number }[]; limits: { wind_ms: number; rain_mm: number; tmax_c: number }; note: string }
export interface WeatherFeed {
  available: boolean; stale?: boolean; stale_reason?: string; reason?: string; fetched?: string; note?: string
  observed: { day: string; tmax: number; tmin: number; rain_mm: number; wind_ms: number; rh: number | null }[]
  forecast: { source: string | null; issue?: string; members?: number; reason?: string; days: { day: string; tmax: number[]; tmin: number[]; rain_mm: number[]; wind_ms: number[] }[] }
}
export interface SatelliteFeed { available: boolean; stale?: boolean; reason?: string; series: { date: string; ndvi: number; cover: number }[]; latest: { available: boolean; url?: string; date?: string; cloud?: number }; source?: string }
export interface Today {
  farm: { id: string; name: string; crop: string; area_ha: number | null; mode: Mode }; day: string; das: number
  status: 'spray' | 'wait' | 'scan' | 'none'; code: string; params: Record<string, unknown>
  next: { product: string; due: string; op: string; targets_frac?: number } | null
  window: { day: string; p_flyable: number; wind_dawn_p50?: number; rain_p50?: number } | null
  health: { lai: number | null; dvs: number | null; stage_code: string | null; herbicide_option?: string; scans?: number }
  alerts: Alert[]
}
export interface Device { id: string; farm_id: string; kind: string; name: string; meta: Record<string, unknown>; created_at: string; last_seen: string | null }
export interface Reading { id: number; device_id: string; kind: string; ts: string; value: number | null; unit: string | null; payload: Record<string, unknown> }
export interface RecordRow { id: string; ts: string; source: string; product: string | null; litres: number | null; area_ha: number | null; operation_id: string | null; payload: Record<string, unknown>; missing: string[] }

// ------------------------------------------------------------------ jobs: SSE with a polling fallback
export function watchJob(id: string, onEvent: (j: Partial<Job> & { status: Job['status'] }) => void): () => void {
  let closed = false
  let timer: number | undefined
  const terminal = (s: string) => s === 'done' || s === 'failed' || s === 'cancelled'
  const poll = async () => {
    if (closed) return
    try {
      const j = await api.get<Job>(`/api/jobs/${id}`)
      onEvent(j)
      if (terminal(j.status)) return
    } catch {
      /* keep polling */
    }
    timer = window.setTimeout(poll, 1200)
  }
  let es: EventSource | null = null
  try {
    es = new EventSource(`/api/jobs/${id}/events`)
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data)
      onEvent(ev)
      if (terminal(ev.status)) es?.close()
    }
    es.onerror = () => {
      es?.close()
      es = null
      if (!closed) poll()
    }
  } catch {
    poll()
  }
  return () => {
    closed = true
    es?.close()
    if (timer) window.clearTimeout(timer)
  }
}

// ------------------------------------------------------------------ binary arrays in the scene payloads
function bytes(b64: string): Uint8Array {
  const s = atob(b64)
  const out = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i)
  return out
}
export function f32(b64: string): Float32Array {
  const u = bytes(b64)
  return new Float32Array(u.buffer, u.byteOffset, u.byteLength / 4)
}
export function f16(b64: string): Float32Array {
  const u = bytes(b64)
  const h = new Uint16Array(u.buffer, u.byteOffset, u.byteLength / 2)
  const out = new Float32Array(h.length)
  for (let i = 0; i < h.length; i++) {
    const v = h[i]
    const s = v & 0x8000 ? -1 : 1
    const e = (v >> 10) & 0x1f
    const f = v & 0x3ff
    out[i] = e === 0 ? s * 2 ** -14 * (f / 1024) : e === 31 ? (f ? NaN : s * Infinity) : s * 2 ** (e - 15) * (1 + f / 1024)
  }
  return out
}
