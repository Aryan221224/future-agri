import { useEffect, useRef, useState } from 'react'
import { api, type LiveRunState, type Scene } from '../api'
import { useApp, useFarm } from '../state'
import { href } from '../router'
import { Icon } from '../ui/icons'
import { Badge, Empty, fmt, Notice, Stat } from '../ui/core'
import { FarmScene } from '../three/FarmScene'
import { cssVar, isDark } from './Twin3D'

const COLORS = { remaining: '#f2a541', seen: '#3aa36b', deviation: '#d64545' }

/** One spray job flown as a closed loop: the drone, what its camera sees, where the field
 * differs from the prediction, and the rest of the job as it is re-planned. */
export default function LiveRun() {
  const { farm, refresh } = useFarm()
  const { toast, resolvedTheme } = useApp()
  const [run, setRun] = useState<LiveRunState | null>(null)
  const [missing, setMissing] = useState(false)
  const [scene, setScene] = useState<Scene | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sceneRef = useRef<FarmScene | null>(null)
  const fid = farm?.id

  useEffect(() => {
    if (!fid) return
    api.get<Scene>(`/api/farms/${fid}/scene`).then(setScene).catch((e) => toast(`3D scene: ${e.message}`, 'bad'))
  }, [fid, toast])

  useEffect(() => {
    if (!fid) return
    let alive = true
    let timer = 0
    const poll = async () => {
      try {
        const r = await api.get<LiveRunState>(`/api/farms/${fid}/live-run`)
        if (!alive) return
        setRun(r)
        setMissing(false)
        if (r.status === 'running') timer = window.setTimeout(poll, 1000)
        else refresh()
      } catch {
        if (alive) setMissing(true)
      }
    }
    poll()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [fid, refresh])

  useEffect(() => {
    if (!scene || !canvasRef.current || !farm) return
    const s = new FarmScene(canvasRef.current, scene, {
      crop: farm.crop, imageryUrl: scene.imagery.available ? scene.imagery.url : null, dark: isDark(), bg: cssVar('--scene-bg'),
    })
    s.setCanopyVisible(false)
    sceneRef.current = s
    return () => { s.dispose(); sceneRef.current = null }
  }, [scene, farm, resolvedTheme])

  useEffect(() => {
    const s = sceneRef.current
    if (!s || !run) return
    const dev = Object.values(run.deviation ?? {}).flat()
    s.setMarkers([
      { cells: run.remaining, color: COLORS.remaining, lift: 0.15, size: 0.9 },
      { cells: run.detections.map((d) => d.cell), color: COLORS.seen, lift: 0.25, size: 0.55 },
      { cells: dev, color: COLORS.deviation, lift: 0.35, size: 0.3 },
    ])
    s.showRoute(run.track && run.track.length > 1 ? run.track : null)
    s.showDroneAt(run.drone)
    s.showFootprint(run.drone, run.footprint_m)
  }, [run, scene, resolvedTheme])

  if (!farm) return null
  if (missing && !run) {
    return (
      <div className="page">
        <Empty title="No live run on this farm" action={<a className="btn" href={href.farm(farm.id, 'mission')}>Back to the mission</a>}>
          Start one from a spray job on the Mission page.
        </Empty>
      </div>
    )
  }

  const aircraft = run?.source === 'aircraft'
  const busy = !run || run.status !== 'running'
  const freeze = async () => {
    try { setRun(await api.post<LiveRunState>(`/api/farms/${farm.id}/live-run/freeze`)); toast('Plan frozen: open loop') } catch (e) { toast((e as Error).message, 'bad') }
  }
  const finish = async () => {
    try { setRun(await api.post<LiveRunState>(`/api/farms/${farm.id}/live-run/finish`)); toast('What the camera saw is now in the twin') } catch (e) { toast((e as Error).message, 'bad') }
  }
  const last = run?.audit.filter((a) => a.channel === 'plan').slice(-1)[0]

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Live run</h1>
          <p className="muted">
            {run ? <>{run.product} job <span className="num">{run.op_id}</span> — {run.source === 'aircraft' ? 'messages from the aircraft' : 'simulated camera on the sprayer'}{run.valve ? ', spot valve' : ''}</> : 'Loading…'}
          </p>
        </div>
        <div className="btn-row">
          {run ? <Badge kind={run.mode === 'closed loop' ? 'live' : 'warn'}>{run.mode}</Badge> : null}
          {run ? <Badge kind={run.status === 'running' ? 'live' : run.status === 'not flown' ? 'bad' : 'ok'}>{run.status}</Badge> : null}
        </div>
      </div>

      {run?.status === 'not flown' ? <Notice kind="warn">Not flown: {run.reason}</Notice> : null}
      {last ? <Notice kind="ok">{last.correction === 'job complete' ? 'Job complete' : last.correction}: {last.reason}</Notice> : null}

      <div className="liverun">
        <div className="viewport liverun-view">
          <canvas ref={canvasRef} aria-label={`Live run over ${farm.name}`} role="img" />
          <div className="overlay liverun-legend" aria-label="Legend">
            <div><i style={{ background: COLORS.remaining }} />Remaining targets</div>
            <div><i style={{ background: COLORS.seen }} />Seen by the camera</div>
            <div><i style={{ background: COLORS.deviation }} />Differs from the prediction</div>
            <div><i style={{ background: '#2b6cb0', height: 2 }} />Camera footprint</div>
          </div>
        </div>

        <div className="liverun-side">
          <section className="sheet sheet-pad">
            <h3>Planned and flown</h3>
            <div className="stats">
              <Stat value={run?.planned ? `${fmt.n(run.planned.litres, 1)} L` : '–'} label="Planned" note={run?.planned ? `${fmt.count(run.planned.sorties, 'sortie', 'sorties', 0)}, ${fmt.minutes(run.planned.time_min)}` : undefined} />
              <Stat value={run?.flown ? `${fmt.n(run.flown.litres, 1)} L` : '–'} label="Flown" note={run?.flown ? `${fmt.count(run.flown.sorties, 'sortie', 'sorties', 0)}, ${fmt.minutes(run.flown.time_min)}` : undefined} />
              <Stat value={run ? run.cells_seen : '–'} label="Cells seen" note={run ? `${run.remaining.length} cells left to spray` : undefined} />
            </div>
          </section>

          <section className="sheet sheet-pad">
            <h3>Operator</h3>
            <div className="btn-row" style={{ marginTop: 8 }}>
              <button className="btn btn-s" disabled={busy || !aircraft || run?.mode === 'open loop'} onClick={freeze}>
                <Icon name="pause" size={16} />Freeze plan (open loop)
              </button>
              {aircraft && run?.status === 'running'
                ? <a className="btn btn-s" href={`/api/farms/${farm.id}/live-run/next-sortie.kml`}><Icon name="download" size={16} />Download next sortie (KML)</a>
                : <button className="btn btn-s" disabled><Icon name="download" size={16} />Download next sortie (KML)</button>}
              {aircraft ? <button className="btn btn-primary btn-s" disabled={busy} onClick={finish}><Icon name="check" size={16} />Finish and update the twin</button> : null}
            </div>
            <p className="xs muted" style={{ marginTop: 8 }}>
              {aircraft
                ? 'Nothing is sent to the aircraft: load the next sortie file in its app. Freezing stops all corrections; the approved plan stands.'
                : 'Freezing and the next-sortie file are for runs flown by the aircraft; the simulated camera re-plans every sortie by itself.'}
            </p>
          </section>
        </div>
      </div>

      <section className="sheet sheet-pad" style={{ marginTop: 18 }}>
        <h3>Sorties</h3>
        <div style={{ overflowX: 'auto' }}>
          <table className="table" aria-label="Sortie log">
            <thead><tr><th>Sortie</th><th>Cells seen</th><th>Cells sprayed</th><th>Litres</th><th>Minutes</th><th>Correction</th><th>Left</th></tr></thead>
            <tbody>
              {(run?.sorties ?? []).map((r) => (
                <tr key={r.n}><td className="num">{r.n}</td><td className="num">{r.cells_seen}</td><td className="num">{r.cells_sprayed}</td>
                  <td className="num">{fmt.n(r.litres, 2)}</td><td className="num">{fmt.n(r.time_min, 1)}</td><td>{r.correction}</td><td className="num">{r.remaining}</td></tr>
              ))}
              {aircraft ? <tr><td colSpan={7} className="muted">{run?.batches ?? 0} message batches received</td></tr> : null}
            </tbody>
          </table>
        </div>
      </section>

      <section className="sheet sheet-pad" style={{ marginTop: 18 }}>
        <h3>Corrections and why</h3>
        {run && run.audit.length ? (
          <ul className="audit">
            {run.audit.map((a, i) => (
              <li key={i}><b>{a.sortie !== undefined ? `Sortie ${a.sortie}` : a.batch !== undefined ? `Batch ${a.batch}` : 'Operator'} · {a.channel}</b> — {a.correction}<div className="xs muted">{a.reason}</div></li>
            ))}
          </ul>
        ) : <p className="muted small">No correction yet: the field matches the prediction.</p>}
      </section>
    </div>
  )
}
