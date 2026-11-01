import { LOCALE } from '../i18n'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type GeoJSON, type Job, type Op, type Outlook, type SessionEvent, type SessionState, type TraceRow } from '../api'
import { useApp, useFarm } from '../state'
import { href } from '../router'
import { Icon } from '../ui/icons'
import { Badge, Dialog, Field, fmt, JobBar, Notice, Progress, Segmented, STATUS_KIND, useJob } from '../ui/core'

type Act = { result: unknown; state: SessionState }
const STAGES: [string, number][] = [['emergence', 0.05], ['vegetative', 1.0], ['flowering', 1.3], ['filling', 2.0], ['mature', 9]]

export default function Mission() {
  const { farm, session, setSession, refresh, sessionError } = useFarm()
  const { t, toast } = useApp()
  const [trace, setTrace] = useState<TraceRow[]>([])
  const [events, setEvents] = useState<SessionEvent[]>([])
  const [jobId, setJobId] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [outlook, setOutlook] = useState<Outlook | null>(null)
  const [iterDone, setIterDone] = useState<boolean | null>(null)
  const [record, setRecord] = useState<Op | null>(null)
  const [upload, setUpload] = useState(false)
  const [score, setScore] = useState<Record<string, unknown> | null>(null)
  const fid = farm?.id
  const nTrace = session?.n_trace ?? 0

  useEffect(() => {
    if (!fid) return
    api.get<{ trace: TraceRow[]; events: SessionEvent[] }>(`/api/farms/${fid}/session/trace`)
      .then((r) => { setTrace(r.trace); setEvents(r.events) }).catch(() => undefined)
  }, [fid, nTrace, session?.n_events])
  useEffect(() => {
    if (!fid) return
    api.get<Outlook>(`/api/farms/${fid}/live/outlook`).then(setOutlook).catch(() => setOutlook(null))
  }, [fid, session?.day])
  useEffect(() => {
    if (!fid) return
    api.get<{ jobs: Job[] }>(`/api/farms/${fid}/iterations`).then((r) => setIterDone(r.jobs.some((j) => j.status === 'done'))).catch(() => setIterDone(false))
  }, [fid])

  const job = useJob(jobId, async (j) => {
    await refresh()
    setJobId(null)
    if (j.status === 'failed') toast(j.error ?? 'The run failed', 'bad')
  })

  const act = useCallback(async (path: string, body: unknown = {}, done?: string) => {
    if (!fid) return
    setBusy(true)
    try {
      const r = await api.post<Act>(`/api/farms/${fid}/session/${path}`, body)
      setSession(r.state)
      if (done) toast(done)
    } catch (e) {
      toast((e as Error).message, 'bad')
    } finally {
      setBusy(false)
    }
  }, [fid, setSession, toast])

  const run = async (body: Record<string, unknown>) => {
    if (!fid) return
    try {
      const j = await api.post<Job>(`/api/farms/${fid}/session/run`, body)
      setJobId(j.id)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }

  if (!farm) return null
  if (sessionError) return <Notice kind="bad">The mission could not start: {sessionError}</Notice>
  if (!session) return <div className="page" style={{ maxWidth: 420 }}><JobBar job={{ status: 'running', progress: 0, message: 'Loading the mission (replaying its log)' }} /></div>

  const s = session
  const field = farm.mode === 'field'
  const proposed = s.ops.filter((o) => o.status === 'proposed')
  const approved = s.ops.filter((o) => o.status === 'approved')
  const running = !!jobId
  const lastRow = trace[trace.length - 1]
  const plusDays = (d: string, n: number) => { const x = new Date(d + 'T12:00:00'); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10) }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Mission</h1>
          <p className="muted">
            {s.plan.name ? <>Plan <b>{s.plan.name}</b> ({s.policy})</> : <>Default plan: the twin decides ({s.policy}). <a href={href.farm(farm.id, 'iterate')}>Run the iterations</a> to choose a better one.</>}
          </p>
        </div>
        <div className="btn-row">
          <Badge kind={field ? 'live' : 'info'}>{t(`mode.${farm.mode}`)}</Badge>
          {s.done ? <Badge kind="ok">season complete</Badge> : null}
        </div>
      </div>

      <LoopTrack s={s} iterDone={iterDone} field={field} />

      <div className="sheet day-bar" style={{ margin: '18px 0' }}>
        <div>
          <div className="day-big">{fmt.date(s.day)}</div>
          <div className="muted small">
            day {s.das} of {Math.round((new Date(s.harvest).getTime() - new Date(s.sowing).getTime()) / 86400000)}
            {' — '}{s.done ? 'season complete' : s.world.waiting ? 'waiting for new weather data' : s.phase === 'flights' ? 'decisions waiting before today\'s flights' : 'morning'}
          </div>
          {lastRow ? (
            <div className="small faint num" style={{ marginTop: 4 }}>
              Yesterday {lastRow.tmin.toFixed(0)}–{lastRow.tmax.toFixed(0)} °C, rain {lastRow.rain_mm.toFixed(1)} mm, wind {lastRow.wind_ms.toFixed(1)} m/s
            </div>
          ) : null}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignItems: 'flex-end', minWidth: 280 }}>
          {s.done ? null : (
            <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn" disabled={busy || running} onClick={() => act('step')}><Icon name="skip" size={18} />Next day</button>
              <button className="btn btn-primary" disabled={busy || running} onClick={() => run({ stop_on_decision: true })}><Icon name="forward" size={18} />Run to the next decision</button>
              <button className="btn" disabled={busy || running} onClick={() => run({ until: plusDays(s.day, 6), stop_on_decision: true })}>Run 7 days</button>
              {!field ? <button className="btn btn-quiet" disabled={busy || running} title="Approves every recommendation on the way"
                onClick={() => run({ stop_on_decision: false, approve_all: true })}>Run to harvest</button> : null}
            </div>
          )}
          {field ? (
            <div className="btn-row" style={{ justifyContent: 'flex-end' }}>
              <button className="btn btn-s" disabled={busy || running} onClick={() => act('refresh', {}, 'Weather updated')}><Icon name="refresh" size={16} />Fetch new weather</button>
              <button className="btn btn-s" disabled={busy || running} onClick={() => setUpload(true)}><Icon name="upload" size={16} />Upload a scan</button>
            </div>
          ) : null}
          {job ? <div style={{ width: '100%' }}><JobBar job={job} label="Running the season" /></div> : null}
        </div>
      </div>
      {s.world.waiting ? <Notice>{s.world.waiting}. The mission continues when NASA POWER publishes the next day.</Notice> : null}

      <div className="cols-main" style={{ marginTop: 18 }}>
        <div>
          <section className="section">
            <div className="section-head"><h2>Decisions</h2><span className="xs muted">The twin proposes; you decide. Sprays fly only when approved.</span></div>
            {proposed.length || approved.length ? (
              <div className="rows">
                {[...proposed, ...approved].map((o) => (
                  <OpCard key={o.id} op={o} field={field} busy={busy || running} fid={farm.id}
                    onApprove={() => act('approve', { op_id: o.id }, `${o.product} approved`)}
                    onReject={() => act('reject', { op_id: o.id, reason: 'operator decision' }, `${o.product} rejected`)}
                    onDefer={(d) => act('defer', { op_id: o.id, days: d }, `Deferred ${d} days`)}
                    onFly={() => act('fly', { op_id: o.id }, 'Flight attempted')}
                    onRecord={() => setRecord(o)} onUpload={() => setUpload(true)} />
                ))}
              </div>
            ) : (
              <p className="muted small">Nothing waits for a decision. {s.done ? '' : 'Run the season; the twin will propose scans and sprays when they pay off.'}</p>
            )}
          </section>
          <section className="section">
            <div className="section-head"><h2>Timeline</h2><span className="xs muted">{events.length} events</span></div>
            <Timeline events={events.length ? events : s.recent_events} />
          </section>
        </div>
        <aside>
          <GoNoGo outlook={outlook} field={field} />
          <TwinPanel s={s} />
          <section className="section">
            <h3 style={{ marginBottom: 8 }}>Season</h3>
            <Ribbon s={s} trace={trace} events={events} />
          </section>
          <section className="section">
            <h3 style={{ marginBottom: 8 }}>So far</h3>
            <table className="table"><tbody>
              <tr><td>Missions flown</td><td className="n">{s.missions}</td></tr>
              <tr><td>Litres sprayed</td><td className="n">{fmt.n(s.litres, 1)}</td></tr>
              <tr><td>Scans</td><td className="n">{s.scans}</td></tr>
              {Object.entries(s.costs).map(([k, v]) => <tr key={k}><td className="muted">{k.replace(/_/g, ' ')} (per ha)</td><td className="n">{fmt.n(v, 2)}</td></tr>)}
            </tbody></table>
            {s.notes.length ? <ul className="xs muted" style={{ paddingLeft: 16, marginTop: 8 }}>{s.notes.slice(-6).map((n) => <li key={n}>{n}</li>)}</ul> : null}
          </section>
          {s.done && !field ? (
            <section className="section">
              <h3>Score the season</h3>
              <p className="small muted" style={{ margin: '4px 0 10px' }}>Compare against the virtual farm's hidden ground truth at harvest.</p>
              <button className="btn btn-primary" disabled={busy} onClick={async () => {
                try { setScore(await api.post(`/api/farms/${farm.id}/session/finish`)) } catch (e) { toast((e as Error).message, 'bad') }
              }}>Harvest and score</button>
              {score ? <ScoreCard r={score} /> : null}
            </section>
          ) : null}
        </aside>
      </div>
      {record ? <RecordDialog op={record} onClose={() => setRecord(null)} onSave={(rec) => { act('record', { op_id: record.id, record: rec }, 'Flight recorded'); setRecord(null) }} day={s.day} /> : null}
      {upload ? <UploadDialog fid={farm.id} onDetected={refresh} onClose={() => setUpload(false)} onUpload={async (gj, gsd) => {
        setUpload(false)
        try {
          const r = await api.post<{ report: { points: number; weeds: number; disease: number; outside: number; bad: number }; state: SessionState }>(`/api/farms/${farm.id}/session/upload-scan`, { geojson: gj, gsd_m: gsd })
          setSession(r.state)
          toast(`Scan ingested: ${r.report.weeds} weeds, ${r.report.disease} disease spots${r.report.outside ? `, ${r.report.outside} points outside the field ignored` : ''}`)
        } catch (e) { toast((e as Error).message, 'bad') }
      }} /> : null}
    </>
  )
}

function LoopTrack({ s, iterDone, field }: { s: SessionState; iterDone: boolean | null; field: boolean }) {
  const scanOp = s.ops.find((o) => o.product === 'scan' && o.status === 'proposed')
  const open = s.ops.filter((o) => o.product !== 'scan' && (o.status === 'proposed' || o.status === 'approved'))
  const flown = s.ops.filter((o) => o.product !== 'scan' && o.status === 'flown')
  const scanDay = new Date(new Date(s.sowing).getTime() + s.scan_das * 86400000).toISOString().slice(0, 10)
  const steps = [
    { name: 'Scan', note: s.scans ? `${s.scans} done` : scanOp ? 'upload due' : `due ${fmt.short(scanDay)}`, done: s.scans > 0, now: !!scanOp },
    { name: 'Digital twin', note: s.twin?.lai !== null && s.twin?.lai !== undefined ? `LAI ${s.twin.lai.toFixed(1)}` : 'built', done: true, now: false },
    { name: 'Iterate', note: iterDone ? 'done' : iterDone === null ? '–' : 'not run', done: !!iterDone, now: false },
    { name: 'Plan', note: s.plan.name ?? 'default', done: !!s.plan.id, now: false },
    { name: 'Fly', note: open.length ? `${open.length} open` : 'nothing due', done: false, now: open.length > 0 && !scanOp },
    { name: 'Record', note: field ? `${flown.length} recorded` : `${flown.length} flown`, done: flown.length > 0, now: false },
    { name: 'Update', note: s.done ? 'season complete' : `twin to ${fmt.short(s.world.has_truth ? new Date(new Date(s.day).getTime() - 86400000).toISOString().slice(0, 10) : s.world.last_observed)}`, done: s.done, now: !scanOp && !open.length && !s.done },
  ]
  return (
    <ol className="loop" style={{ listStyle: 'none', padding: 0, margin: 0 }} aria-label="Mission loop">
      {steps.map((st, i) => (
        <li key={st.name} className={`loop-step${st.now ? ' now' : st.done ? ' done' : ''}`} aria-current={st.now ? 'step' : undefined}>
          <i>{i + 1}</i><b>{st.name}</b><span>{st.note}</span>
        </li>
      ))}
    </ol>
  )
}

function OpCard({ op, field, busy, fid, onApprove, onReject, onDefer, onFly, onRecord, onUpload }: {
  op: Op; field: boolean; busy: boolean; fid: string; onApprove: () => void; onReject: () => void; onDefer: (d: number) => void
  onFly: () => void; onRecord: () => void; onUpload: () => void
}) {
  const { t } = useApp()
  const pct = Math.round(op.targets_frac * 100)
  return (
    <div className="op">
      <div>
        <h4>
          <span style={{ textTransform: 'capitalize' }}>{op.product === 'scan' ? 'Camera-drone scan' : op.product}</span>
          <Badge kind={STATUS_KIND[op.status]}>{t(`status.${op.status}`)}</Badge>
        </h4>
        <p className="small muted" style={{ marginTop: 2 }}>{op.reason}</p>
        <p className="xs faint num" style={{ marginTop: 4 }}>
          Due {fmt.date(op.due)}{op.product !== 'scan' ? ` — ${pct === 100 ? 'whole field' : `${pct}% of the field (spot spray)`}` : ''}
        </p>
      </div>
      <div className="btn-row" style={{ justifyContent: 'flex-end', alignContent: 'flex-start' }}>
        {op.product === 'scan' ? (
          <button className="btn btn-primary btn-s" disabled={busy} onClick={onUpload}><Icon name="upload" size={16} />Upload the scan</button>
        ) : op.status === 'proposed' ? (
          <>
            <button className="btn btn-primary btn-s" disabled={busy} onClick={onApprove}><Icon name="check" size={16} />{t('act.approve')}</button>
            <select className="input" style={{ width: 'auto', minHeight: 32 }} aria-label="Defer by" disabled={busy} value=""
              onChange={(e) => { if (e.target.value) onDefer(Number(e.target.value)) }}>
              <option value="">{t('act.defer')}…</option><option value="1">1 day</option><option value="3">3 days</option><option value="7">7 days</option>
            </select>
            <button className="btn btn-quiet btn-s" disabled={busy} onClick={onReject}>{t('act.reject')}</button>
          </>
        ) : field ? (
          <>
            <a className="btn btn-s" href={`/api/farms/${fid}/export/kml?op=${op.id}`}><Icon name="download" size={16} />KML</a>
            <a className="btn btn-s" href={`/api/farms/${fid}/export/settings`} target="_blank" rel="noreferrer">Settings</a>
            <button className="btn btn-fly btn-s" disabled={busy} onClick={onRecord}>{t('act.record')}</button>
          </>
        ) : (
          <button className="btn btn-fly btn-s" disabled={busy} onClick={onFly}><Icon name="drone" size={16} />{t('act.fly')}</button>
        )}
      </div>
    </div>
  )
}

const KIND_DOT: Record<string, string> = { flight: 'flight', scan: 'scan', plan: 'plan', proposal: 'warn', approved: 'plan', no_fly: 'warn', dropped: 'bad', rejected: 'bad', waiting: 'warn', scout_needed: 'warn' }
const KIND_NAME: Record<string, string> = { flight: 'flown', scan: 'scan', plan: 'twin plan', proposal: 'proposed', approved: 'approved', no_fly: 'no flight', dropped: 'dropped', rejected: 'rejected', deferred: 'deferred', waiting: 'waiting', scouting: 'scouting', scout_needed: 'scouting asked' }

function Timeline({ events }: { events: SessionEvent[] }) {
  const items = [...events].reverse().slice(0, 60)
  if (!items.length) return <p className="muted small">The season has not started. Use Next day or Run to the next decision.</p>
  return (
    <ul className="timeline">
      {items.map((e, i) => (
        <li key={i}>
          <span className="when">{fmt.short(e.day)}</span>
          <span className={`dot ${KIND_DOT[e.kind] ?? ''}`} aria-hidden="true" />
          <span><b>{KIND_NAME[e.kind] ?? e.kind}</b>{' — '}{e.text}</span>
        </li>
      ))}
    </ul>
  )
}

function GoNoGo({ outlook, field }: { outlook: Outlook | null; field: boolean }) {
  const d = outlook?.available ? outlook.days[0] : null
  const go = d ? d.p_flyable >= 0.5 : null
  return (
    <section className="section" style={{ paddingTop: 0 }}>
      <h3 style={{ marginBottom: 10 }}>Spray window today</h3>
      {d ? (
        <div className="gonogo">
          <span className="sig" style={{ background: go ? 'var(--canopy)' : 'var(--alarm)' }} aria-hidden="true"><Icon name={go ? 'check' : 'x'} /></span>
          <div>
            <b>{go ? 'Flyable' : 'Not flyable'}</b> <span className="muted small">({Math.round(d.p_flyable * 100)}% of forecast members)</span>
            <div className="xs muted num">Dawn wind {d.wind_dawn_p50} m/s (limit {outlook!.limits.wind_ms}), rain {d.rain_p50} mm, max {d.tmax_p50} °C</div>
            <div className="xs faint">{outlook!.source} forecast issued {fmt.date(outlook!.issue)}{field ? '' : ' (as on the session day)'}</div>
          </div>
        </div>
      ) : <p className="small muted">{outlook?.reason ?? 'No forecast available.'}</p>}
    </section>
  )
}

function TwinPanel({ s }: { s: SessionState }) {
  const tw = s.twin
  const stage = tw?.dvs !== null && tw?.dvs !== undefined ? STAGES.find(([, lim]) => tw.dvs! < lim)?.[0] : null
  const { t } = useApp()
  return (
    <section className="section">
      <h3 style={{ marginBottom: 8 }}>Twin estimate</h3>
      {tw ? (
        <div className="stats">
          <div className="stat"><b>{tw.lai !== null ? tw.lai.toFixed(2) : '–'}</b><span>Leaf area index</span></div>
          <div className="stat"><b>{stage ? t(`stage.${stage}`) : '–'}</b><span>Stage</span><small className="num">DVS {tw.dvs?.toFixed(2) ?? '–'}</small></div>
          <div className="stat"><b>{tw.twso !== null ? Math.round(tw.twso).toLocaleString(LOCALE) : '–'}</b><span>Yield so far</span><small>kg dry matter/ha</small></div>
        </div>
      ) : <p className="small muted">This plan runs without the twin ({s.policy}).</p>}
      {tw ? <p className="xs faint" style={{ marginTop: 6 }}>Herbicide decision: {tw.herbicide_option}; scans ingested: {tw.scans}.</p> : null}
    </section>
  )
}

function Ribbon({ s, trace, events }: { s: SessionState; trace: TraceRow[]; events: SessionEvent[] }) {
  const t0 = new Date(s.sowing).getTime(), t1 = new Date(s.harvest).getTime()
  const x = (d: string) => `${Math.min(100, Math.max(0, ((new Date(d).getTime() - t0) / (t1 - t0)) * 100))}%`
  const stages = useMemo(() => {
    const out: { name: string; from: string; to: string }[] = []
    for (const r of trace) {
      if (r.twin_dvs === null) continue
      const name = STAGES.find(([, lim]) => r.twin_dvs! < lim)?.[0] ?? 'mature'
      if (!out.length || out[out.length - 1].name !== name) out.push({ name, from: r.day, to: r.day })
      else out[out.length - 1].to = r.day
    }
    return out
  }, [trace])
  const { t } = useApp()
  return (
    <div>
      <div className="ribbon" aria-label="Season ribbon">
        {stages.map((st) => (
          <div key={st.name + st.from} className="stage" style={{ left: x(st.from), width: `calc(${x(st.to)} - ${x(st.from)})`, background: 'var(--canopy-soft)' }}>{t(`stage.${st.name}`)}</div>
        ))}
        {s.ops.filter((o) => o.product !== 'scan' && (o.status === 'approved' || o.status === 'proposed')).map((o) => (
          <span key={o.id} className="mark" title={`${o.product} due ${o.due}`} style={{ left: x(o.due), background: 'var(--amber)' }} />
        ))}
        {events.filter((e) => e.kind === 'flight' || e.kind === 'scan').map((e, i) => (
          <span key={i} className="mark" title={`${e.kind} ${e.day}`} style={{ left: x(e.day), background: e.kind === 'flight' ? 'var(--survey)' : 'var(--sky)' }} />
        ))}
        <span className="today" style={{ left: x(s.day) }} title={`today ${s.day}`} />
      </div>
      <div className="ribbon-scale"><span>Sown {fmt.short(s.sowing)}</span><span>Harvest {fmt.short(s.harvest)}</span></div>
      <p className="xs faint" style={{ marginTop: 4 }}><span style={{ color: 'var(--survey)' }}>▮</span> flights <span style={{ color: 'var(--sky)' }}>▮</span> scans <span style={{ color: 'var(--amber)' }}>▮</span> jobs due</p>
    </div>
  )
}

function ScoreCard({ r }: { r: Record<string, unknown> }) {
  const n = (k: string) => Number(r[k] ?? 0)
  return (
    <div className="stats" style={{ marginTop: 12 }}>
      <div className="stat"><b>{Math.round(n('yield_marketed_kg_ha')).toLocaleString(LOCALE)}</b><span>Yield (kg/ha marketed)</span></div>
      <div className="stat"><b>{Math.round(n('net_ha')).toLocaleString(LOCALE)}</b><span>Net {String(r.currency)}/ha</span></div>
      <div className="stat"><b>{n('fungicide_sprays')}</b><span>Fungicide sprays</span></div>
      <div className="stat"><b>{fmt.n(n('weed_loss_pct'), 1)}%</b><span>Yield lost to weeds</span></div>
    </div>
  )
}

function RecordDialog({ op, onClose, onSave, day }: { op: Op; onClose: () => void; onSave: (r: Record<string, unknown>) => void; day: string }) {
  const [rec, setRec] = useState({ day, litres: '', area_ha: '', wind_ms: '1.5', wind_dir_deg: '270', temp_c: '18', duration_min: '' })
  const num = (k: keyof typeof rec, label: string) => (
    <Field label={label}><input className="input num" type={k === 'day' ? 'date' : 'number'} value={rec[k]} onChange={(e) => setRec({ ...rec, [k]: e.target.value })} /></Field>
  )
  return (
    <Dialog title={`Record the ${op.product} flight`} onClose={onClose}
      actions={<><button className="btn" onClick={onClose}>Cancel</button>
        <button className="btn btn-primary" disabled={!rec.litres || !rec.day} onClick={() => onSave({
          day: rec.day, litres: Number(rec.litres), area_ha: rec.area_ha ? Number(rec.area_ha) : undefined, wind_ms: Number(rec.wind_ms),
          wind_dir_deg: Number(rec.wind_dir_deg), temp_c: Number(rec.temp_c), duration_min: rec.duration_min ? Number(rec.duration_min) : undefined,
        })}>Save the record</button></>}>
      <p className="small muted">From the K3-A Pro job record (Agri Assistant). The twin models where the spray landed from these values and updates the field.</p>
      <div className="grid-form" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
        {num('day', 'Flight date')}{num('litres', 'Litres sprayed')}{num('area_ha', 'Area sprayed (ha)')}{num('duration_min', 'Flight time (min)')}
        {num('wind_ms', 'Wind (m/s)')}{num('wind_dir_deg', 'Wind from (°)')}{num('temp_c', 'Temperature (°C)')}
      </div>
    </Dialog>
  )
}

function UploadDialog({ fid, onClose, onUpload, onDetected }: {
  fid: string; onClose: () => void; onUpload: (gj: GeoJSON, gsd: number) => void; onDetected: () => void
}) {
  const { toast } = useApp()
  const [kind, setKind] = useState<'geojson' | 'ortho'>('geojson')
  const [gj, setGj] = useState<GeoJSON | null>(null)
  const [gsd, setGsd] = useState('5')
  const [err, setErr] = useState<string | null>(null)
  const [tif, setTif] = useState<File | null>(null)
  const [sent, setSent] = useState<number | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [gpu, setGpu] = useState<boolean | null>(null)
  useEffect(() => {
    api.get<{ remote: { alive: boolean; kinds: string[] }[] }>('/api/workers')
      .then((w) => setGpu(w.remote.some((x) => x.alive && x.kinds.includes('detect')))).catch(() => setGpu(false))
  }, [])
  const job = useJob(jobId, (j) => {
    if (j.status === 'done') { toast(j.message || 'Weed detections imported'); onDetected() }
    else if (j.status === 'failed') toast(j.error ?? 'Weed detection failed', 'bad')
  })
  const sendOrtho = () => {
    if (!tif) return
    const fd = new FormData()
    fd.append('file', tif)
    const xhr = new XMLHttpRequest()               // fetch cannot report upload progress
    xhr.open('POST', `/api/farms/${fid}/session/ortho`)
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) setSent(e.loaded / e.total) }
    xhr.onload = () => {
      let body: { job?: Job; detail?: string } = {}
      try { body = JSON.parse(xhr.responseText) } catch { /* not JSON */ }
      if (xhr.status === 200 && body.job) { setJobId(body.job.id); setSent(1) }
      else { setSent(null); setErr(body.detail ?? `upload failed (HTTP ${xhr.status})`) }
    }
    xhr.onerror = () => { setSent(null); setErr('upload failed: the console did not answer') }
    setErr(null)
    setSent(0)
    xhr.send(fd)
  }
  const busy = sent !== null && !jobId
  const actions = kind === 'geojson'
    ? <><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={!gj} onClick={() => gj && onUpload(gj, Number(gsd) / 1000)}>Ingest the scan</button></>
    : jobId
      ? <button className="btn btn-primary" onClick={onClose}>{job?.status === 'done' || job?.status === 'failed' ? 'Close' : 'Keep working (it continues)'}</button>
      : <><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={!tif || busy || gpu === false} onClick={sendOrtho}>Detect weeds</button></>
  return (
    <Dialog title="Upload a camera-drone scan" onClose={onClose} actions={actions}>
      <Segmented label="What you have" value={kind} onChange={(v) => { setKind(v); setErr(null) }}
        options={[{ value: 'geojson', label: 'Detections (GeoJSON)' }, { value: 'ortho', label: 'Orthomosaic (GeoTIFF)' }]} />
      {kind === 'geojson' ? (
        <>
          <p className="small muted">GeoJSON points from the detector (class <code>weed</code> or <code>disease</code>, optional <code>confidence</code> and <code>severity</code>). Counts are corrected with the detector's measured recall at this ground sampling distance.</p>
          <Field label="Detections file (GeoJSON)">
            <input className="input" type="file" accept=".geojson,.json" onChange={async (e) => {
              const f = e.target.files?.[0]
              if (!f) return
              try {
                const j = JSON.parse(await f.text())
                if (j.type !== 'FeatureCollection' && j.type !== 'Feature') throw new Error('not a GeoJSON FeatureCollection')
                setGj(j); setErr(null)
              } catch (x) { setErr((x as Error).message); setGj(null) }
            }} />
          </Field>
          <Field label="Ground sampling distance (mm per pixel)"><input className="input num" type="number" min={1} max={100} value={gsd} onChange={(e) => setGsd(e.target.value)} /></Field>
        </>
      ) : (
        <>
          <p className="small muted">The stitched orthomosaic from the survey flight (DJI Terra, WebODM, Pix4D…), exported as a GeoTIFF with its coordinates.
            The trained RF-DETR finds the weeds on the GPU server; the detections are imported into the mission when it finishes.</p>
          {gpu === false ? <Notice kind="warn">No GPU server with the weed detector is connected. Connect it from System, then upload.</Notice> : null}
          <Field label="Orthomosaic (GeoTIFF)" hint={tif ? `${(tif.size / 1e6).toFixed(0)} MB` : undefined}>
            <input className="input" type="file" accept=".tif,.tiff" disabled={busy || !!jobId} onChange={(e) => { setTif(e.target.files?.[0] ?? null); setErr(null) }} />
          </Field>
          {sent !== null && !jobId ? <div><Progress value={sent} /><p className="xs muted" style={{ marginTop: 4 }}>Uploading {Math.round(sent * 100)}%</p></div> : null}
          {job ? <JobBar job={job} label="Weed detection" /> : null}
        </>
      )}
      {err ? <Notice kind="bad">{err}</Notice> : null}
    </Dialog>
  )
}

