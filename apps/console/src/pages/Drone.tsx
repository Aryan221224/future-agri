import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type DroneSpec, type FlightSettings, type Job, type MissionResult, type Outlook, type Scene, type Weather } from '../api'
import { useApp, useFarm } from '../state'
import { Icon } from '../ui/icons'
import { Field, fmt, Notice, Segmented, useJob } from '../ui/core'
import { buildDrone, DroneStudio } from '../three/DroneModel'
import { FarmScene } from '../three/FarmScene'
import { Playback, type Gauges } from '../three/Playback'
import { cssVar, isDark } from './Twin3D'

const DEFAULT_FLIGHT: FlightSettings = {
  height_m: 2.5, speed_ms: 5, swath_m: 4, rate_l_ha: 15, rtk: false, pump_lpm: 3.5, route_angle_deg: null,
  ferry_speed_ms: 8, n_battery_sets: 4, chargers: 1, refill_s: 60, battery_swap_s: 60, battery_cycles: 0,
}
const DEFAULT_WEATHER: Weather = { wind_ms: 1.5, wind_dir_deg: 270, gust_ms: 2.4, temp_c: 20 }
const KPIS: [string, string, (v: number) => string][] = [
  ['time_min', 'Job time', (v) => fmt.minutes(v)], ['flight_min', 'Flying', (v) => fmt.minutes(v)],
  ['litres', 'Litres', (v) => fmt.n(v, 1)], ['sorties', 'Sorties', (v) => String(Math.round(v))],
  ['battery_swaps', 'Battery swaps', (v) => String(Math.round(v))], ['refills', 'Refills', (v) => String(Math.round(v))],
  ['ha_per_hour', 'Work rate (ha/h)', (v) => fmt.n(v, 2)], ['cv_pct', 'Unevenness (CV %)', (v) => fmt.n(v, 1)],
  ['skipped_frac', 'Under-dosed area', (v) => fmt.pct(v, 1)], ['off_target_frac', 'Drift off target', (v) => fmt.pct(v, 1)],
  ['wh_per_ha', 'Energy (Wh/ha)', (v) => fmt.n(v, 0)], ['max_range_m', 'Furthest from pilot (m)', (v) => fmt.n(v, 0)],
]

export default function Drone() {
  const { farm, session } = useFarm()
  const { toast } = useApp()
  const [spec, setSpec] = useState<DroneSpec | null>(null)
  const [scene, setScene] = useState<Scene | null>(null)
  const [flight, setFlight] = useState<FlightSettings>(DEFAULT_FLIGHT)
  const [weather, setWeather] = useState<Weather>(DEFAULT_WEATHER)
  const [view, setView] = useState<'aircraft' | 'farm'>('aircraft')
  const [res, setRes] = useState<MissionResult | null>(null)
  const [prev, setPrev] = useState<MissionResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [jobId, setJobId] = useState<string | null>(null)
  const [g, setG] = useState<Gauges | null>(null)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(30)
  const [scale, setScale] = useState(6)
  const [follow, setFollow] = useState(false)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const farmRef = useRef<FarmScene | null>(null)
  const pbRef = useRef<Playback | null>(null)
  const fid = farm?.id

  useEffect(() => { api.get<DroneSpec>('/api/drone/spec').then(setSpec).catch((e) => toast(e.message, 'bad')) }, [toast])
  useEffect(() => { if (fid) api.get<Scene>(`/api/farms/${fid}/scene`).then(setScene).catch(() => undefined) }, [fid])
  useEffect(() => { if (session?.plan?.flight) setFlight({ ...DEFAULT_FLIGHT, ...session.plan.flight }) }, [session?.plan?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // the aircraft on its turntable
  useEffect(() => {
    if (view !== 'aircraft' || !spec || !canvasRef.current) return
    const s = new DroneStudio(canvasRef.current, spec, cssVar('--scene-bg'))
    return () => s.dispose()
  }, [view, spec])

  // the farm with the mission flown over it
  useEffect(() => {
    if (view !== 'farm' || !scene || !spec || !farm || !canvasRef.current || !res?.feasible) return
    const fs = new FarmScene(canvasRef.current, scene, {
      crop: farm.crop, imageryUrl: scene.imagery.available ? scene.imagery.url : null, dark: isDark(), bg: cssVar('--scene-bg'),
    })
    fs.setLayer('canopy')
    fs.setCanopyVisible(false)
    const pb = new Playback(fs, buildDrone(spec), res, spec.tank_l, scale)
    pb.speed = speed
    pb.onFrame = throttle(setG, 100)
    pb.seek(0)
    farmRef.current = fs
    pbRef.current = pb
    setPlaying(false)
    return () => { pb.dispose(); fs.dispose(); farmRef.current = null; pbRef.current = null }
  }, [view, scene, spec, farm, res]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => { if (pbRef.current) pbRef.current.speed = speed }, [speed])
  useEffect(() => { pbRef.current?.setScale(scale) }, [scale])
  useEffect(() => {
    const fs = farmRef.current, pb = pbRef.current
    if (!fs || !pb) return
    fs.follow(follow ? (pb as unknown as { drone: { group: import('three').Object3D } }).drone.group : null)
  }, [follow, res])
  useEffect(() => {
    if (!playing) return
    const h = window.setInterval(() => { if (pbRef.current && !pbRef.current.playing) setPlaying(false) }, 300)
    return () => window.clearInterval(h)
  }, [playing])

  const finish = useCallback((r: MissionResult) => {
    setRes((old) => { if (old?.feasible) setPrev(old); return r })
    if (r.feasible) setView('farm')
  }, [])

  useJob(jobId, async (j) => {
    setBusy(false)
    if (j.status === 'done' && j.id) finish(await api.get<MissionResult>(`/api/jobs/${j.id}/result`))
    else if (j.status === 'failed') toast(j.error ?? 'The mission failed', 'bad')
  })

  const simulate = async () => {
    if (!fid) return
    setBusy(true)
    try {
      const r = await api.post<MissionResult | { job: Job }>(`/api/farms/${fid}/mission`, { settings: flight, weather })
      if ('job' in r) { setJobId(r.job.id); return }
      finish(r)
      setBusy(false)
    } catch (e) {
      setBusy(false)
      toast((e as Error).message, 'bad')
    }
  }

  const useForecast = async () => {
    if (!fid) return
    try {
      const o = await api.get<Outlook>(`/api/farms/${fid}/live/outlook`)
      const d = o.days?.[0]
      if (!o.available || !d) { toast(o.reason ?? 'No forecast available', 'bad'); return }
      setWeather((w) => ({ ...w, wind_ms: Number(d.wind_dawn_p50.toFixed(1)), gust_ms: Number((d.wind_dawn_p50 * 1.6).toFixed(1)), temp_c: Number((d.tmax_p50 - 4).toFixed(0)) }))
      toast(`Weather from the ${o.source} forecast issued ${fmt.date(o.issue)} (dawn wind)`)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }

  if (!farm) return null
  const num = (k: keyof FlightSettings, label: string, step: number, min: number, max: number) => (
    <Field label={label}>
      <input className="input num" type="number" step={step} min={min} max={max} value={String(flight[k] ?? '')}
        onChange={(e) => setFlight({ ...flight, [k]: e.target.value === '' ? null : Number(e.target.value) })} />
    </Field>
  )
  const wnum = (k: keyof Weather, label: string, step: number, min: number, max: number) => (
    <Field label={label}>
      <input className="input num" type="number" step={step} min={min} max={max} value={weather[k]}
        onChange={(e) => setWeather({ ...weather, [k]: Number(e.target.value) })} />
    </Field>
  )

  return (
    <div className="drone-layout">
      <div className="viewport">
        <canvas ref={canvasRef} key={view} aria-label={view === 'aircraft' ? 'The K3-A Pro drone in 3D' : `Mission flown over ${farm.name}`} role="img" />
        <div className="overlay" style={{ left: 16, top: 16, padding: 6 }}>
          <Segmented label="View" value={view} onChange={(v) => setView(v)}
            options={[{ value: 'aircraft', label: 'Aircraft' }, { value: 'farm', label: res?.feasible ? 'Over the farm' : 'Over the farm (simulate first)' }]} />
        </div>
        {view === 'aircraft' && spec ? <SpecCallouts spec={spec} /> : null}
        {view === 'farm' && res?.feasible ? (
          <>
            <div className="overlay drone-tools">
              <label className="field"><span>Drone size ×{scale} (for visibility)</span>
                <input type="range" min={1} max={12} value={scale} onChange={(e) => setScale(Number(e.target.value))} /></label>
              <label className="check small"><input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />Follow the drone</label>
            </div>
            <GaugePanel g={g} tank={spec?.tank_l ?? 10} />
            <div className="overlay drone-time">
              <button className="btn btn-fly btn-s" aria-label={playing ? 'Pause the flight' : 'Play the flight'}
                onClick={() => { const pb = pbRef.current; if (!pb) return; if (pb.playing) { pb.pause(); setPlaying(false) } else { pb.play(); setPlaying(true) } }}>
                <Icon name={playing ? 'pause' : 'play'} size={16} />
              </button>
              <select className="input" style={{ width: 'auto', minHeight: 32 }} aria-label="Playback speed" value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
                {[1, 10, 30, 60, 120].map((s) => <option key={s} value={s}>{s}×</option>)}
              </select>
              <input type="range" min={0} max={Math.max(1, g?.duration ?? 1)} step={1} value={g?.t ?? 0} aria-label="Mission time"
                onChange={(e) => { pbRef.current?.seek(Number(e.target.value)) }} style={{ flex: 1 }} />
              <span className="num small" style={{ minWidth: 92, textAlign: 'right' }}>{clock(g?.t ?? 0)} / {clock(g?.duration ?? 0)}</span>
            </div>
          </>
        ) : null}
      </div>

      <aside className="drone-side">
        <section className="section">
          <div className="section-head"><h2>Mission planner</h2>{session?.plan?.name ? <span className="xs muted">from plan {session.plan.name}</span> : null}</div>
          <div className="grid-form" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            {num('height_m', 'Height (m)', 0.5, 1, 10)}
            {num('speed_ms', 'Speed (m/s)', 0.5, 1, 10)}
            {num('swath_m', 'Row spacing (m)', 0.5, 1, 10)}
            {num('rate_l_ha', 'Rate (L/ha)', 1, 1, 100)}
            {num('pump_lpm', 'Pump (L/min)', 0.5, 0.5, 10)}
            {num('route_angle_deg', 'Route angle (°)', 5, 0, 180)}
            {num('n_battery_sets', 'Battery sets', 1, 1, 12)}
            {num('chargers', 'Chargers', 1, 1, 6)}
          </div>
          <label className="check"><input type="checkbox" checked={flight.rtk} onChange={(e) => setFlight({ ...flight, rtk: e.target.checked })} />RTK positioning (centimetre accuracy)</label>
          <div className="section-head" style={{ marginTop: 8 }}><h3>Weather</h3><button className="btn btn-quiet btn-s" onClick={useForecast}><Icon name="wind" size={16} />Use the forecast</button></div>
          <div className="grid-form" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
            {wnum('wind_ms', 'Wind (m/s)', 0.1, 0, 20)}
            {wnum('wind_dir_deg', 'Wind from (°)', 10, 0, 360)}
            {wnum('gust_ms', 'Gusts (m/s)', 0.1, 0, 30)}
            {wnum('temp_c', 'Temperature (°C)', 1, -20, 50)}
          </div>
          <button className="btn btn-primary btn-l" style={{ width: '100%', marginTop: 12 }} disabled={busy} onClick={simulate}>
            <Icon name="drone" />{busy ? 'Flying the twin' : 'Simulate mission'}
          </button>
          <p className="xs faint" style={{ marginTop: 6 }}>The drone twin flies the job the K3-A Pro would: serpentine passes, refills, battery swaps and the spray pattern under this wind.</p>
        </section>

        {res ? <Results res={res} prev={prev} onSeek={(t) => { setView('farm'); pbRef.current?.seek(t) }} /> : null}
        {spec ? <SpecSheet spec={spec} /> : null}
      </aside>
    </div>
  )
}

function Results({ res, prev, onSeek }: { res: MissionResult; prev: MissionResult | null; onSeek: (t: number) => void }) {
  if (!res.feasible) {
    return (
      <section className="section">
        <h2>This mission cannot be flown</h2>
        <Notice kind="bad">{res.reasons.join('; ')}</Notice>
      </section>
    )
  }
  const k = res.kpis
  return (
    <section className="section">
      <div className="section-head"><h2>Result</h2><span className="xs muted">{fmt.n(k.area_ha, 2)} ha at {fmt.n(k.achieved_rate_l_ha, 1)} L/ha</span></div>
      {res.warnings.map((w) => <Notice key={w} kind="warn">{w}</Notice>)}
      <div className="kpi-grid">
        {KPIS.map(([key, label, f]) => (
          <div key={key} className="kpi">
            <b data-testid={`kpi-${key.replace('_frac', '').replace('_min', '').replace(/_/g, '-')}`}>{f(k[key])}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>
      {prev ? (
        <div className="table-wrap" style={{ marginTop: 14 }}>
          <table className="table">
            <thead><tr><th>Compared with the previous run</th><th className="n">Now</th><th className="n">Before</th></tr></thead>
            <tbody>
              {(['time_min', 'litres', 'sorties', 'cv_pct', 'skipped_frac', 'off_target_frac', 'ha_per_hour'] as const).map((key) => {
                const row = KPIS.find((x) => x[0] === key)!
                return <tr key={key}><td>{row[1]}</td><td className="n">{row[2](k[key])}</td><td className="n">{row[2](prev.kpis[key])}</td></tr>
              })}
            </tbody>
          </table>
        </div>
      ) : null}
      {res.events.length ? (
        <>
          <h3 style={{ margin: '14px 0 6px' }}>What happened in the air</h3>
          <ul className="events">
            {res.events.slice(0, 40).map((e, i) => (
              <li key={i}><button className="btn btn-quiet btn-s" onClick={() => onSeek(Number(e.t_s))}>
                <span className="num">{clock(Number(e.t_s))}</span> {eventText(e)}</button></li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  )
}

function eventText(e: Record<string, unknown>): string {
  const reason = e.reason ? ` (${e.reason})` : ''
  switch (e.event) {
    case 'rth': return `Return to refill or swap the battery${reason}`
    case 'ground': return `On the ground ${fmt.n(Number(e.ground_s), 0)} s${reason}`
    case 'resume': return 'Resumed at the breakpoint'
    case 'relocate_home': return `Crew moves the take-off point${reason}`
    case 'landed': return 'Job complete, landed'
    default: return String(e.event)
  }
}

function GaugePanel({ g, tank }: { g: Gauges | null; tank: number }) {
  if (!g) return null
  return (
    <div className="overlay gauges" aria-label="Drone gauges">
      <Gauge label="Battery" value={`${Math.round(g.soc * 100)}%`} frac={g.soc} note={`${g.v_cell.toFixed(2)} V/cell`} warn={g.soc < 0.25} />
      <Gauge label="Tank" value={`${g.liquid_l.toFixed(1)} L`} frac={g.liquid_l / tank} note={`of ${tank} L`} />
      <div className="gauge"><span>Flow</span><b className="num">{g.flow_lpm.toFixed(2)}</b><small>L/min</small></div>
      <div className="gauge"><span>Speed</span><b className="num">{g.speed_ms.toFixed(1)}</b><small>m/s</small></div>
      <div className="gauge"><span>Height</span><b className="num">{g.z.toFixed(1)}</b><small>m</small></div>
      <div className="gauge"><span>Sortie</span><b className="num">{g.sortie}</b><small>{phaseText(g.phase, g.spraying)}</small></div>
      <div className="gauge"><span>Elapsed</span><b className="num" data-testid="gauge-elapsed">{clock(g.t)}</b><small>mission time</small></div>
    </div>
  )
}

function Gauge({ label, value, frac, note, warn }: { label: string; value: string; frac: number; note: string; warn?: boolean }) {
  return (
    <div className="gauge">
      <span>{label}</span><b className="num">{value}</b>
      <div className="progress" style={{ height: 6 }}><i style={{ width: `${Math.max(0, Math.min(1, frac)) * 100}%`, background: warn ? 'var(--alarm)' : undefined }} /></div>
      <small>{note}</small>
    </div>
  )
}

function phaseText(p: string, spraying: boolean): string {
  if (spraying) return 'spraying'
  return ({ takeoff: 'take-off', ferry: 'flying', turn: 'turning', rth: 'returning', land: 'landing', ground: 'refill / swap', relocate: 'moving base', landed: 'landed', spray: 'spraying' } as Record<string, string>)[p] ?? p
}

function SpecCallouts({ spec }: { spec: DroneSpec }) {
  return (
    <div className="overlay" style={{ left: 16, bottom: 16, maxWidth: 360 }}>
      <h4>{spec.controller} sprayer, built to its spec sheet</h4>
      <table className="table" style={{ fontSize: 'var(--fs-xs)' }}><tbody>
        <tr><td className="muted">Wheelbase</td><td className="n">{spec.wheelbase_mm} mm (orange circle)</td></tr>
        <tr><td className="muted">Rotors</td><td className="n">{spec.n_rotors} × {Math.round(spec.rotor_diameter_m * 1000)} mm</td></tr>
        <tr><td className="muted">Unfolded</td><td className="n">{spec.unfolded_mm.join(' × ')} mm</td></tr>
        <tr><td className="muted">Tank</td><td className="n">{spec.tank_l} L, {spec.n_nozzles} nozzles</td></tr>
        <tr><td className="muted">Mass</td><td className="n">{spec.mass_kg.empty} kg empty, {spec.mass_kg.standard} kg loaded</td></tr>
      </tbody></table>
    </div>
  )
}

function SpecSheet({ spec }: { spec: DroneSpec }) {
  const rows: [string, string][] = [
    ['Aircraft', spec.name], ['Flight controller', spec.controller],
    ['Hover time', spec.hover_points.map(([m, t]) => `${t} min at ${m} kg`).join('; ')],
    ['Battery', `${spec.battery.cells}S ${spec.battery.capacity_ah} Ah, ${spec.battery.nominal_wh} Wh${spec.battery.inferred ? ' (inferred)' : ''}`],
    ['Pump', `${spec.pump_lpm.join('–')} L/min`], ['Swath', `${spec.swath_m.join('–')} m (table ${spec.swath_table_m} m)`],
    ['Work speed', `up to ${spec.limits.work_speed_ms} m/s`], ['Wind limit', `${spec.limits.wind_ms} m/s, gusts ${spec.limits.gust_ms} m/s`],
    ['Radio range', `${spec.link_range_m} m`], ['Charging', `${spec.charge_min} min per pack`],
    ['Route groups', `${spec.limits.waypoints_per_group} waypoints each`], ['Position accuracy', `±${spec.limits.hover_accuracy_m} m without RTK`],
  ]
  return (
    <section className="section">
      <h2 style={{ marginBottom: 8 }}>Spec sheet</h2>
      <table className="table"><tbody>{rows.map(([k, v]) => <tr key={k}><td className="muted">{k}</td><td>{v}</td></tr>)}</tbody></table>
      <ul className="small muted" style={{ paddingLeft: 18, margin: '10px 0 0' }}>{spec.notes.map((n) => <li key={n}>{n}</li>)}</ul>
    </section>
  )
}

function clock(s: number): string {
  const m = Math.floor(s / 60), r = Math.floor(s % 60)
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`
}

function throttle<T>(fn: (v: T) => void, ms: number): (v: T) => void {
  let last = 0
  let pending: T | null = null
  let timer: number | undefined
  return (v: T) => {
    const now = performance.now()
    if (now - last >= ms) { last = now; fn(v); return }
    pending = v
    if (timer === undefined) timer = window.setTimeout(() => { timer = undefined; last = performance.now(); if (pending !== null) fn(pending) }, ms)
  }
}
