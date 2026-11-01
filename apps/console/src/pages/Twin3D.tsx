import { useCallback, useEffect, useRef, useState } from 'react'
import { api, f16, type Scene, type SessionEvent } from '../api'
import { useApp, useFarm } from '../state'
import { href } from '../router'
import { Icon } from '../ui/icons'
import { fmt } from '../ui/core'
import { FarmScene, type Cats, type CellInfo, type Layer, type Legend, type Snapshot } from '../three/FarmScene'
import { cssGradient } from '../three/ramps'

type Snaps = { days: string[]; data: Record<string, Record<string, string>> }

const LAYERS: { id: Layer; label: string; needsSnap?: boolean }[] = [
  { id: 'canopy', label: 'Crop', needsSnap: true },
  { id: 'lai', label: 'Leaf area', needsSnap: true },
  { id: 'weeds', label: 'Weeds', needsSnap: true },
  { id: 'blight', label: 'Blight', needsSnap: true },
  { id: 'dose', label: 'Sprayed', needsSnap: true },
  { id: 'zone', label: 'Zones' },
  { id: 'twi', label: 'Wetness' },
]

export function isDark(): boolean {
  const attr = document.documentElement.getAttribute('data-theme')
  if (attr) return attr === 'dark'
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false
}

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#dfe7e2'
}

export function decodeSnap(d: Record<string, string>): Snapshot {
  return {
    lai: f16(d.lai_b64), cc: f16(d.cc_b64), weeds: f16(d.weeds_b64), blight: f16(d.blight_b64),
    dose_fungicide: f16(d.dose_fungicide_b64), dose_herbicide: f16(d.dose_herbicide_b64),
  }
}

export default function Twin3D() {
  const { farm, session } = useFarm()
  const { toast } = useApp()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sceneRef = useRef<FarmScene | null>(null)
  const decoded = useRef(new Map<string, Snapshot>())
  const [scene, setScene] = useState<Scene | null>(null)
  const [snaps, setSnaps] = useState<Snaps | null>(null)
  const [events, setEvents] = useState<SessionEvent[]>([])
  const [dayIdx, setDayIdx] = useState(0)
  const [layer, setLayer] = useState<Layer>('canopy')
  const [cell, setCell] = useState<CellInfo | null>(null)
  const [exag, setExag] = useState(2)
  const [canopyK, setCanopyK] = useState(4)
  const [imagery, setImagery] = useState(true)
  const [playing, setPlaying] = useState(false)
  const [legend, setLegend] = useState<Legend | Cats | null>(null)
  const fid = farm?.id
  const nTrace = session?.n_trace ?? 0

  useEffect(() => {
    if (!fid) return
    api.get<Scene>(`/api/farms/${fid}/scene`).then(setScene).catch((e) => toast(`3D scene: ${e.message}`, 'bad'))
  }, [fid, toast])

  useEffect(() => {
    if (!fid) return
    api.get<Snaps>(`/api/farms/${fid}/session/snapshots`).then((s) => {
      decoded.current.clear()
      setSnaps(s)
      setDayIdx(Math.max(0, s.days.length - 1))
      if (!s.days.length) setLayer('zone')
    }).catch(() => setSnaps({ days: [], data: {} }))
    api.get<{ events: SessionEvent[] }>(`/api/farms/${fid}/session/trace`).then((r) => setEvents(r.events)).catch(() => undefined)
  }, [fid, nTrace])

  useEffect(() => {
    if (!scene || !canvasRef.current || !farm) return
    const s = new FarmScene(canvasRef.current, scene, {
      crop: farm.crop, imageryUrl: scene.imagery.available ? scene.imagery.url : null, dark: isDark(), bg: cssVar('--scene-bg'),
    })
    s.onPick = setCell
    sceneRef.current = s
    return () => { s.dispose(); sceneRef.current = null }
  }, [scene, farm])

  const snapFor = useCallback((day: string | undefined): Snapshot | null => {
    if (!day || !snaps?.data[day]) return null
    if (!decoded.current.has(day)) decoded.current.set(day, decodeSnap(snaps.data[day]))
    return decoded.current.get(day)!
  }, [snaps])

  const day = snaps?.days[dayIdx]
  useEffect(() => {
    const s = sceneRef.current
    if (!s) return
    s.setSnapshot(snapFor(day))
    s.setLayer(layer)
    setLegend(s.legend())
  }, [day, layer, snapFor, scene])

  useEffect(() => { sceneRef.current?.setExaggeration(exag) }, [exag])
  useEffect(() => { sceneRef.current?.setCanopyScale(canopyK) }, [canopyK])
  useEffect(() => { sceneRef.current?.setImagery(imagery) }, [imagery])

  useEffect(() => {
    if (!playing || !snaps) return
    const h = window.setInterval(() => setDayIdx((i) => {
      if (i >= snaps.days.length - 1) { setPlaying(false); return i }
      return i + 1
    }), 450)
    return () => window.clearInterval(h)
  }, [playing, snaps])

  if (!farm) return null
  const days = snaps?.days ?? []
  const hasSnaps = days.length > 0
  const truth = farm.mode === 'sim'
  const das = day && session ? Math.round((new Date(day).getTime() - new Date(session.sowing).getTime()) / 86400000) : null
  const span = days.length > 1 ? new Date(days[days.length - 1]).getTime() - new Date(days[0]).getTime() : 1
  const marks = hasSnaps ? events.filter((e) => e.kind === 'flight' || e.kind === 'scan').map((e) => ({
    kind: e.kind, day: e.day, x: Math.min(1, Math.max(0, (new Date(e.day).getTime() - new Date(days[0]).getTime()) / span)),
  })) : []

  return (
    <div className="twin">
      <div className="viewport">
        <canvas ref={canvasRef} aria-label={`3D model of ${farm.name}`} role="img" />
      </div>

      <section className="overlay twin-layers" aria-label="Layers">
        <h4>Layer</h4>
        <div role="radiogroup" aria-label="Layer">
          {LAYERS.map((l) => (
            <label key={l.id} className="radio" aria-disabled={l.needsSnap && !hasSnaps}>
              <input type="radio" name="layer" checked={layer === l.id} disabled={(l.needsSnap && !hasSnaps) || (l.id === 'twi' && !scene?.layers.twi_b64)}
                onChange={() => setLayer(l.id)} />
              {l.label}
            </label>
          ))}
        </div>
        {legend ? <LegendView legend={legend} /> : null}
        <p className="xs faint" style={{ marginTop: 8 }}>
          {layer === 'zone' ? scene?.layer_meta.zone : layer === 'twi' ? scene?.layer_meta.twi
            : truth ? 'Virtual farm ground truth: what the twin has to discover from scans.' : 'From the latest scan of the field.'}
        </p>
      </section>

      <section className="overlay twin-tools" aria-label="View">
        <div className="btn-row">
          <button className="btn btn-s" onClick={() => sceneRef.current?.resetView()}>Reset view</button>
          <button className="btn btn-s" onClick={() => sceneRef.current?.topView()}>Top view</button>
        </div>
        <label className="field" style={{ marginTop: 10 }}>
          <span>Terrain relief ×{exag}</span>
          <input type="range" min={1} max={10} step={1} value={exag} onChange={(e) => setExag(Number(e.target.value))} />
        </label>
        <label className="field">
          <span>Crop height ×{canopyK}</span>
          <input type="range" min={1} max={12} step={1} value={canopyK} onChange={(e) => setCanopyK(Number(e.target.value))} />
        </label>
        <label className="check small">
          <input type="checkbox" checked={imagery} disabled={!scene?.imagery.available} onChange={(e) => setImagery(e.target.checked)} />
          Satellite image
        </label>
        <p className="xs faint">
          {scene?.imagery.available ? `Sentinel-2, ${fmt.date(scene.imagery.date)}` : 'No clear satellite image saved'}; terrain Copernicus DEM
          {scene ? ` (${(scene.terrain.max - scene.terrain.min).toFixed(1)} m relief)` : ''}.
        </p>
      </section>

      {cell ? <Inspector cell={cell} onClose={() => { sceneRef.current?.pick(-1) }} truth={truth} /> : null}

      {hasSnaps ? (
        <section className="overlay twin-time" aria-label="Season">
          <button className="btn btn-s" aria-label={playing ? 'Pause' : 'Play the season'} onClick={() => {
            if (!playing && dayIdx >= days.length - 1) setDayIdx(0)
            setPlaying(!playing)
          }}><Icon name={playing ? 'pause' : 'play'} size={16} /></button>
          <div style={{ minWidth: 150 }}>
            <b className="num">{fmt.date(day)}</b>
            <div className="xs muted">{das !== null ? `day ${das} of the season` : ''}</div>
          </div>
          <div style={{ flex: 1, position: 'relative', paddingTop: 10 }}>
            <div aria-hidden="true" style={{ position: 'absolute', left: 0, right: 0, top: 0, height: 8 }}>
              {marks.map((m, i) => (
                <span key={i} title={`${m.kind} on ${m.day}`} style={{
                  position: 'absolute', left: `${m.x * 100}%`, width: 3, height: 8, borderRadius: 2,
                  background: m.kind === 'flight' ? 'var(--survey)' : 'var(--sky)',
                }} />
              ))}
            </div>
            <input type="range" min={0} max={Math.max(0, days.length - 1)} value={dayIdx} aria-label="Day of the season"
              onChange={(e) => { setPlaying(false); setDayIdx(Number(e.target.value)) }} />
          </div>
          <span className="xs muted" style={{ whiteSpace: 'nowrap' }}>
            <span style={{ color: 'var(--survey)' }}>▮</span> flights <span style={{ color: 'var(--sky)', marginLeft: 6 }}>▮</span> scans
          </span>
        </section>
      ) : (
        <section className="overlay twin-empty">
          <h3>The crop appears as the season runs</h3>
          <p className="small muted" style={{ margin: '6px 0 12px' }}>Terrain, the field and its zones are shown now. Run the mission and
            the twin's season fills this view day by day.</p>
          <a className="btn btn-primary" href={href.farm(farm.id, 'mission')}>Open the mission</a>
        </section>
      )}
    </div>
  )
}

function LegendView({ legend }: { legend: Legend | Cats }) {
  if (legend.kind === 'cats') {
    return (
      <div style={{ marginTop: 10 }}>
        <div className="xs muted">{legend.label}</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px 10px', marginTop: 4 }}>
          {legend.items.map((it) => (
            <span key={it.label} className="xs" style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
              <span style={{ width: 11, height: 11, borderRadius: 3, background: it.color, display: 'inline-block' }} />{it.label}
            </span>
          ))}
        </div>
      </div>
    )
  }
  const d = legend.max < 1 ? 2 : legend.max < 10 ? 1 : 0
  return (
    <div style={{ marginTop: 10 }}>
      <div className="xs muted">{legend.label} ({legend.unit})</div>
      <div className="legend-bar" style={{ background: cssGradient(legend.ramp) }} />
      <div className="legend-scale"><span>{legend.min.toFixed(d)}</span><span>{legend.max.toFixed(d)}</span></div>
    </div>
  )
}

function Inspector({ cell, onClose, truth }: { cell: CellInfo; onClose: () => void; truth: boolean }) {
  const rows: [string, string][] = [
    ['Position', `${cell.x.toFixed(0)} m E, ${cell.y.toFixed(0)} m N of the field corner`],
    ['Zone', String(cell.zone)],
  ]
  if (cell.twi !== undefined) rows.push(['Wetness index', cell.twi.toFixed(1)])
  if (cell.lai !== undefined) {
    rows.push(['Leaf area index', cell.lai.toFixed(2)], ['Ground cover', fmt.pct(cell.cc)],
      ['Weeds', `${(cell.weeds ?? 0).toFixed(1)} plants/m²`], ['Late blight', fmt.pct(cell.blight, 1)],
      ['Fungicide so far', `${(cell.dose_fungicide ?? 0).toFixed(1)} L/ha`], ['Herbicide so far', `${(cell.dose_herbicide ?? 0).toFixed(1)} L/ha`])
  }
  return (
    <section className="overlay twin-inspector" aria-label="Cell details">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <h4>Cell {cell.index + 1}</h4>
        <button className="btn btn-quiet btn-s" aria-label="Close cell details" onClick={onClose}><Icon name="x" size={16} /></button>
      </div>
      <table className="table" style={{ fontSize: 'var(--fs-xs)' }}>
        <tbody>{rows.map(([k, v]) => <tr key={k}><td className="muted">{k}</td><td className="n">{v}</td></tr>)}</tbody>
      </table>
      {cell.lai !== undefined && truth ? <p className="xs faint" style={{ marginTop: 6 }}>Ground truth of the virtual farm.</p> : null}
    </section>
  )
}
