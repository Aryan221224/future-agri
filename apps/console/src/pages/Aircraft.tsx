import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import { useApp, useFarm } from '../state'
import { href } from '../router'
import { Icon } from '../ui/icons'
import { Badge, Empty, Field, fmt, Notice, Stat } from '../ui/core'

type Backend = 'kit' | 'stock' | 'ardupilot'

interface AgentView {
  online: boolean; seen_s_ago: number | null; pending: string[]
  acks: { id: string; ok: boolean; error?: string | null }[]
  agent: null | {
    state: string; job_id: string | null; sortie: number; pass: number; spraying: boolean; xtrack_m: number | null
    preflight: string[]; events: [number, string, string][]; agent_error: string | null; queued: number
    fix: null | { kind: string; e: number; n: number; h: number; heading: number; heading_ok: boolean; sats: number }
    bridge: null | { source: string; pilot_ok: boolean; auto_allowed: boolean; errors: number }
    calibration: null | { applied: string[]; kept: Record<string, string>; axes: Record<string, { gain: number; dead: number; tau_s: number }> }
  }
}
interface StockBlock { n: number; cells: number; status: string; area_ha: number; litres_planned: number; records: { litres: number; area_ha: number; source: string }[] }
interface StockState {
  run_id: string; op_id: string; status: string; rate_l_ha: number; tank_l: number; current: number | null
  blocks: StockBlock[]; audit: { block: number; channel: string; correction: string; reason: string }[]
  pushed: { block: number; file: string | null; error: string | null } | null
  flown: { litres: number; blocks_done: number; blocks: number }
  remote: { connected: boolean; model?: string; serial?: string; error?: string } | null
}
interface ApStatus {
  connected: boolean; link: string | null; link_ok: boolean
  vehicle: null | { mode: string; armed: boolean; lat: number | null; lon: number | null; rel_alt: number | null; gps_fix: number; sats: number; hdop: number; voltage: number | null; remaining: number | null; link_age_s: number | null; texts: [number, number, string][] }
  job?: { state: string; sortie: number; flying: boolean; summary: { passes: number; sorties: number; litres: number }; events: { t: number; state: string; reason: string }[]; preflight: { ok: boolean; failures: string[]; warnings: string[] } | null }
}

const LINKS = ['serial:COM5:57600', 'udpin:0.0.0.0:14550', 'tcp:192.168.4.1:5760']

/** Fly the real drone: the K3-A Pro with the autonomy kit, the stock K3-A Pro through Agri
 * Assistant, or an ArduPilot flight controller. */
export default function Aircraft() {
  const { farm, session } = useFarm()
  const { toast } = useApp()
  const [backend, setBackend] = useState<Backend>(() => (localStorage.getItem('fa.aircraft.backend') as Backend) || 'kit')
  const [opId, setOpId] = useState('')
  const fid = farm?.id
  const jobs = (session?.ops ?? []).filter((o) => (o.product === 'herbicide' || o.product === 'fungicide') && (o.status === 'proposed' || o.status === 'approved'))
  useEffect(() => { if (!opId && jobs.length) setOpId(jobs[0].id) }, [jobs, opId])
  useEffect(() => { try { localStorage.setItem('fa.aircraft.backend', backend) } catch { /* private window */ } }, [backend])
  const call = useCallback(async <T,>(p: Promise<T>, ok?: string): Promise<T | null> => {
    try { const r = await p; if (ok) toast(ok); return r } catch (e) { toast((e as Error).message, 'bad'); return null }
  }, [toast])

  if (!farm || !fid) return null
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Aircraft</h1>
          <p className="muted">Fly a spray job with the real drone. Pick how this drone is controlled.</p>
        </div>
      </div>
      <div className="seg" role="tablist" aria-label="How the drone is controlled" style={{ marginBottom: 16 }}>
        {([['kit', 'K3-A Pro + autonomy kit'], ['stock', 'K3-A Pro, stock'], ['ardupilot', 'ArduPilot']] as [Backend, string][]).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={backend === k} className={backend === k ? 'on' : ''} onClick={() => setBackend(k)}>{label}</button>
        ))}
      </div>
      <section className="sheet sheet-pad" style={{ marginBottom: 16 }}>
        <Field label="Spray job" hint={jobs.length ? undefined : <>No open spray job: propose one on the <a href={href.farm(fid, 'mission')}>Mission</a> page.</>}>
          <select value={opId} onChange={(e) => setOpId(e.target.value)} disabled={!jobs.length}>
            {jobs.map((o) => <option key={o.id} value={o.id}>{o.product} · due {o.due} · {Math.round(o.targets_frac * 100)} % of the field · {o.status}</option>)}
          </select>
        </Field>
      </section>
      {backend === 'kit' ? <KitPanel fid={fid} opId={opId} call={call} /> : backend === 'stock' ? <StockPanel fid={fid} opId={opId} call={call} /> : <ArduPanel fid={fid} opId={opId} call={call} />}
    </div>
  )
}

type Call = <T>(p: Promise<T>, ok?: string) => Promise<T | null>

function usePoll<T>(path: string, ms = 1000): [T | null, string | null, () => void] {
  const [v, setV] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [n, setN] = useState(0)
  useEffect(() => {
    let alive = true
    let timer = 0
    const tick = async () => {
      try { const r = await api.get<T>(path); if (alive) { setV(r); setErr(null) } } catch (e) { if (alive) setErr((e as Error).message) }
      if (alive) timer = window.setTimeout(tick, ms)       // keep polling through a failed poll
    }
    tick()
    return () => { alive = false; window.clearTimeout(timer) }
  }, [path, ms, n])
  return [v, err, () => setN((x) => x + 1)]
}

// ------------------------------------------------------------------ K3-A Pro with the kit
function KitPanel({ fid, opId, call }: { fid: string; opId: string; call: Call }) {
  const [v] = usePoll<AgentView>(`/api/farms/${fid}/aircraft/agent`)
  const a = v?.agent
  const cmd = (kind: string, ok: string) => call(api.post(`/api/farms/${fid}/aircraft/agent/command`, { kind, op_id: opId || undefined }), ok)
  const flying = a && !['IDLE', 'DONE', 'FAILED', 'LANDED'].includes(a.state)
  return (
    <>
      {!v?.online ? (
        <Notice kind="warn">The drone's agent is not connected{v?.seen_s_ago != null ? ` (last seen ${fmt.n(v.seen_s_ago, 0)} s ago)` : ''}. Power the drone, check its Wi-Fi/4G, and its token (Live → Add a device, kind <i>drone agent</i>). Setup: docs/hardware/k3a-autonomy-kit.md.</Notice>
      ) : null}
      {a?.agent_error ? <Notice kind="bad">Agent stopped sending: {a.agent_error}. The bridge holds the aircraft, then the K3-A Pro returns home.</Notice> : null}
      {a && a.preflight.length ? <Notice kind="warn">Preflight: {a.preflight.join('; ')}</Notice> : null}
      <div className="stats" style={{ marginBottom: 16 }}>
        <Stat value={a ? a.state : '–'} label="Agent" note={a?.job_id ? `sortie ${a.sortie + 1}, pass ${a.pass + 1}` : undefined} />
        <Stat value={a?.fix ? a.fix.kind : '–'} label="RTK" note={a?.fix ? `${a.fix.sats} sats, heading ${a.fix.heading_ok ? fmt.n(a.fix.heading, 0) + '°' : 'not valid'}` : undefined} />
        <Stat value={a?.bridge ? a.bridge.source : '–'} label="Bridge" note={a?.bridge ? `AUTO switch ${a.bridge.auto_allowed ? 'on' : 'off'}, pilot RX ${a.bridge.pilot_ok ? 'ok' : 'lost'}` : undefined} />
        <Stat value={a?.xtrack_m != null ? `${fmt.n(Math.abs(a.xtrack_m), 2)} m` : '–'} label="Off the line" note={a?.spraying ? 'spraying' : 'pump off'} />
      </div>
      <section className="sheet sheet-pad">
        <h3>Operator</h3>
        <div className="btn-row" style={{ marginTop: 8 }}>
          <button className="btn btn-primary" disabled={!v?.online || !opId || !!flying} onClick={() => cmd('start_job', 'Job sent to the drone')}><Icon name="play" size={16} />Start job</button>
          <button className="btn" disabled={!v?.online || !!flying} onClick={() => cmd('calibrate', 'Calibration flight sent')}><Icon name="target" size={16} />Calibrate</button>
          <button className="btn" disabled={!flying} onClick={() => cmd('pause', 'Pause sent')}><Icon name="pause" size={16} />Pause</button>
          <button className="btn" disabled={a?.state !== 'HOLD'} onClick={() => cmd('resume', 'Resume sent')}><Icon name="forward" size={16} />Resume</button>
          <button className="btn" disabled={a?.state !== 'LANDED'} onClick={() => cmd('battery_swapped', 'Next sortie')}><Icon name="battery" size={16} />Battery swapped</button>
          <button className="btn btn-danger" disabled={!flying} onClick={() => cmd('abort', 'Returning home')}><Icon name="home" size={16} />Abort: return home</button>
        </div>
        <p className="xs muted" style={{ marginTop: 8 }}>The pilot's AUTO switch overrides everything: switch it off to fly by hand. A pause holds position; resuming continues the pass where it stopped.</p>
      </section>
      {a?.calibration ? (
        <section className="sheet sheet-pad" style={{ marginTop: 16 }}>
          <h3>Last calibration</h3>
          <table className="table"><thead><tr><th>Axis</th><th>Full-stick speed</th><th>Dead band</th><th>Response</th></tr></thead>
            <tbody>{Object.entries(a.calibration.axes).map(([k, x]) => <tr key={k}><td>{k}</td><td className="num">{fmt.n(x.gain, 2)}</td><td className="num">{fmt.n(x.dead * 100, 0)} %</td><td className="num">{fmt.n(x.tau_s, 2)} s</td></tr>)}</tbody>
          </table>
        </section>
      ) : null}
      <EventLog rows={(a?.events ?? []).map(([t, s, r]) => ({ t: `${fmt.n(t, 1)} s`, state: s, reason: r }))} />
      {v?.online ? <p className="xs muted" style={{ marginTop: 10 }}>Telemetry and camera counts appear on the <a href={href.farm(fid, 'live-run')}>Live run</a> page.</p> : null}
    </>
  )
}

// ------------------------------------------------------------------ stock K3-A Pro
function StockPanel({ fid, opId, call }: { fid: string; opId: string; call: Call }) {
  const [st, err, again] = usePoll<StockState>(`/api/farms/${fid}/stock-loop`, 2000)
  const [rc, , rcAgain] = usePoll<{ connected: boolean; model?: string; error?: string }>(`/api/rc`, 5000)
  const [tank, setTank] = useState('10')
  const [host, setHost] = useState('')
  const [litres, setLitres] = useState('')
  const [area, setArea] = useState('')
  const running = st?.status === 'running'
  const start = async () => { if (await call(api.post(`/api/farms/${fid}/stock-loop`, { op_id: opId, tank_l: Number(tank) || undefined }), 'Job planned in blocks')) again() }
  const record = async () => {
    if (await call(api.post(`/api/farms/${fid}/stock-loop/record`, { litres: Number(litres), area_ha: Number(area) }), 'Sortie recorded: the twin re-planned the rest')) { setLitres(''); setArea(''); again() }
  }
  return (
    <>
      <Notice>No hardware on the drone: the loop closes after each sortie. Load each block's KML in Agri Assistant, fly it, then give its flight record here (or link the remote to do both).</Notice>
      <section className="sheet sheet-pad" style={{ margin: '16px 0' }}>
        <h3>Remote controller (Agri Assistant)</h3>
        <p className="small">{rc?.connected ? <><Badge kind="ok">linked</Badge> {rc.model}</> : <><Badge kind="warn">not linked</Badge> <span className="muted">{rc?.error}</span></>}</p>
        <div className="btn-row" style={{ marginTop: 8 }}>
          <input aria-label="Remote's Wi-Fi address" placeholder="192.168.1.50 (Wi-Fi ADB)" value={host} onChange={(e) => setHost(e.target.value)} />
          <button className="btn btn-s" disabled={!host} onClick={async () => { await call(api.post('/api/rc/connect', { host }), 'Remote linked'); rcAgain() }}>Connect over Wi-Fi</button>
          <button className="btn btn-s" disabled={!running || !rc?.connected} onClick={async () => { await call(api.post(`/api/farms/${fid}/stock-loop/pull`), 'Records read from the remote'); again() }}><Icon name="refresh" size={16} />Read flight records</button>
        </div>
        <p className="xs muted" style={{ marginTop: 6 }}>By USB: turn on USB debugging on the remote and plug it into this computer. Needs Android platform-tools (adb) on this computer.</p>
      </section>
      {!st || err ? (
        <section className="sheet sheet-pad">
          <h3>Plan the job</h3>
          <div className="btn-row">
            <Field label="Litres per fill"><input type="number" min={0.5} max={100} step={0.5} value={tank} onChange={(e) => setTank(e.target.value)} /></Field>
            <button className="btn btn-primary" disabled={!opId} onClick={start}><Icon name="map" size={16} />Plan in blocks</button>
          </div>
        </section>
      ) : (
        <>
          <div className="stats" style={{ marginBottom: 16 }}>
            <Stat value={`${st.flown.blocks_done} / ${st.flown.blocks}`} label="Blocks done" />
            <Stat value={`${fmt.n(st.flown.litres, 1)} L`} label="Sprayed" note={`${fmt.n(st.rate_l_ha, 1)} L/ha planned`} />
            <Stat value={st.current ?? '–'} label="Next block" note={st.pushed?.file ? 'on the remote' : st.pushed?.error ? 'download it' : undefined} />
          </div>
          {running && st.current ? (
            <section className="sheet sheet-pad">
              <h3>Block {st.current}</h3>
              <div className="btn-row" style={{ marginTop: 8 }}>
                <a className="btn" href={`/api/farms/${fid}/stock-loop/block.kml?n=${st.current}`}><Icon name="download" size={16} />Download block {st.current} (KML)</a>
                <input aria-label="Litres sprayed" type="number" placeholder="Litres sprayed" value={litres} onChange={(e) => setLitres(e.target.value)} />
                <input aria-label="Area sprayed, ha" type="number" placeholder="Area sprayed (ha)" value={area} onChange={(e) => setArea(e.target.value)} />
                <button className="btn btn-primary" disabled={!litres || !area} onClick={record}><Icon name="check" size={16} />Record the sortie</button>
              </div>
            </section>
          ) : null}
          <section className="sheet sheet-pad" style={{ marginTop: 16 }}>
            <h3>Blocks</h3>
            <table className="table"><thead><tr><th>Block</th><th>Area</th><th>Planned</th><th>Flown</th><th>Status</th></tr></thead>
              <tbody>{st.blocks.map((b) => <tr key={b.n}><td className="num">{b.n}</td><td className="num">{fmt.n(b.area_ha, 2)} ha</td><td className="num">{fmt.n(b.litres_planned, 1)} L</td>
                <td className="num">{fmt.n(b.records.reduce((s, r) => s + r.litres, 0), 1)} L</td><td><Badge kind={b.status === 'done' ? 'ok' : b.status === 'partial' ? 'warn' : b.status === 'dropped' ? 'plain' : 'info'}>{b.status}</Badge></td></tr>)}</tbody>
            </table>
            <div className="btn-row" style={{ marginTop: 10 }}>
              <button className="btn btn-primary" disabled={!running} onClick={async () => { await call(api.post(`/api/farms/${fid}/stock-loop/finish`), 'Job booked in the twin'); again() }}><Icon name="check" size={16} />Finish and update the twin</button>
            </div>
          </section>
          <EventLog title="Re-planning and why" rows={st.audit.map((x) => ({ t: `block ${x.block}`, state: x.correction, reason: x.reason }))} />
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------ ArduPilot
function ArduPanel({ fid, opId, call }: { fid: string; opId: string; call: Call }) {
  const [st] = usePoll<ApStatus>(`/api/farms/${fid}/aircraft/ardupilot`)
  const [link, setLink] = useState(LINKS[0])
  const [pre, setPre] = useState<{ ok: boolean; failures: string[]; warnings: string[] } | null>(null)
  const veh = st?.vehicle
  const job = st?.job
  const flying = !!job?.flying
  const base = `/api/farms/${fid}/aircraft/ardupilot`
  return (
    <>
      <section className="sheet sheet-pad" style={{ marginBottom: 16 }}>
        <h3>Link</h3>
        <div className="btn-row" style={{ marginTop: 8 }}>
          <input aria-label="MAVLink link" list="ap-links" value={link} onChange={(e) => setLink(e.target.value)} style={{ minWidth: 240 }} />
          <datalist id="ap-links">{LINKS.map((l) => <option key={l} value={l} />)}</datalist>
          {st?.connected
            ? <button className="btn btn-s" disabled={flying} onClick={() => call(api.post(`${base}/disconnect`), 'Disconnected')}>Disconnect</button>
            : <button className="btn btn-primary btn-s" onClick={() => call(api.post(`${base}/connect`, { link }), 'Flight controller connected')}><Icon name="signal" size={16} />Connect</button>}
        </div>
        <p className="xs muted" style={{ marginTop: 6 }}>USB cable or SiK radio: serial:COMx:57600 (Windows) or serial:/dev/ttyUSB0:57600. Wi-Fi / 4G through the drone's computer: udpin:0.0.0.0:14550 or tcp:IP:5760.</p>
      </section>
      {st?.connected && veh ? (
        <div className="stats" style={{ marginBottom: 16 }}>
          <Stat value={veh.mode} label={veh.armed ? 'Armed' : 'Disarmed'} note={st.link_ok ? 'link ok' : 'LINK LOST'} />
          <Stat value={`${veh.sats} sats`} label="GPS" note={`fix ${veh.gps_fix}, HDOP ${fmt.n(veh.hdop, 1)}`} />
          <Stat value={veh.voltage != null ? `${fmt.n(veh.voltage, 1)} V` : '–'} label="Battery" note={veh.remaining != null ? `${veh.remaining} %` : undefined} />
          <Stat value={veh.rel_alt != null ? `${fmt.n(veh.rel_alt, 1)} m` : '–'} label="Height" />
        </div>
      ) : null}
      {pre ? <Notice kind={pre.ok ? 'ok' : 'bad'}>{pre.ok ? 'Preflight passed' : 'Preflight: ' + pre.failures.join('; ')}{pre.warnings.length ? ' · ' + pre.warnings.join('; ') : ''}</Notice> : null}
      <section className="sheet sheet-pad">
        <h3>Operator</h3>
        <div className="btn-row" style={{ marginTop: 8 }}>
          <button className="btn" disabled={!st?.connected} onClick={async () => setPre(await call(api.get(`${base}/preflight`)))}><Icon name="check" size={16} />Preflight check</button>
          <button className="btn btn-primary" disabled={!st?.connected || !opId || flying} onClick={() => call(api.post(`${base}/start`, { op_id: opId }), 'Job started: upload, arm, take off, AUTO')}><Icon name="play" size={16} />Start job</button>
          <button className="btn" disabled={job?.state !== 'WAIT_SWAP'} onClick={() => call(api.post(`${base}/battery-swapped`), 'Next sortie')}><Icon name="battery" size={16} />Battery swapped</button>
          <button className="btn btn-danger" disabled={!st?.connected} onClick={() => call(api.post(`${base}/abort`), 'RTL sent')}><Icon name="home" size={16} />Abort: return home</button>
        </div>
        <p className="xs muted" style={{ marginTop: 8 }}>ArduPilot's own failsafes (RC, link, battery, EKF, fence) stay in charge in the air. Parameters: services/aircraft/fa_aircraft/params/t1_10l_quad.param.</p>
      </section>
      <EventLog rows={(job?.events ?? []).map((e) => ({ t: new Date(e.t * 1000).toLocaleTimeString('en'), state: e.state, reason: e.reason }))} />
      {veh?.texts?.length ? <EventLog title="Messages from the flight controller" rows={veh.texts.map(([t, sev, txt]) => ({ t: new Date(t * 1000).toLocaleTimeString('en'), state: sev <= 3 ? 'error' : sev <= 4 ? 'warning' : 'info', reason: txt }))} /> : null}
    </>
  )
}

function EventLog({ rows, title = 'What happened' }: { rows: { t: string; state: string; reason: string }[]; title?: string }) {
  return (
    <section className="sheet sheet-pad" style={{ marginTop: 16 }}>
      <h3>{title}</h3>
      {rows.length ? (
        <ul className="audit">{rows.slice().reverse().map((r, i) => <li key={i}><b>{r.t} · {r.state}</b><div className="xs muted">{r.reason}</div></li>)}</ul>
      ) : <Empty title="Nothing yet" />}
    </section>
  )
}
