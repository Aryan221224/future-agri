import { useCallback, useEffect, useState } from 'react'
import { api, type Job, type Plan, type PerfectResult } from '../api'
import { useApp, useFarm } from '../state'
import { go, href } from '../router'
import { Icon } from '../ui/icons'
import { Badge, Empty, Field, fmt, JobBar, Segmented, STATUS_KIND, useJob } from '../ui/core'
import { planName } from './planNames'

interface Workers { local: number; remote: { name: string; alive: boolean; kinds: string[] }[] }

export default function Iterate() {
  const { farm } = useFarm()
  const { toast } = useApp()
  const [jobs, setJobs] = useState<Job[]>([])
  const [res, setRes] = useState<PerfectResult | null>(null)
  const [resJob, setResJob] = useState<Job | null>(null)
  const [seeds, setSeeds] = useState(3)
  const [members, setMembers] = useState(6)
  const [quick, setQuick] = useState(false)
  const [target, setTarget] = useState<'auto' | 'local' | 'remote'>('auto')
  const [workers, setWorkers] = useState<Workers | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [log, setLog] = useState<string[]>([])
  const fid = farm?.id

  const load = useCallback(async () => {
    if (!fid) return
    const r = await api.get<{ jobs: Job[] }>(`/api/farms/${fid}/iterations`)
    setJobs(r.jobs)
    const done = r.jobs.find((j) => j.status === 'done')
    const running = r.jobs.find((j) => j.status === 'running' || j.status === 'queued')
    if (running) setJobId(running.id)
    if (done) { setResJob(done); setRes(await api.get<PerfectResult>(`/api/jobs/${done.id}/result`)) }
  }, [fid])
  useEffect(() => { load().catch((e) => toast(e.message, 'bad')) }, [load, toast])
  useEffect(() => { api.get<Workers>('/api/workers').then(setWorkers).catch(() => undefined) }, [])

  const job = useJob(jobId, async (j) => {
    setJobId(null)
    if (j.status === 'failed') toast(j.error ?? 'Iterations failed', 'bad')
    await load()
  })
  useEffect(() => {
    if (job?.message) setLog((l) => (l[l.length - 1] === job.message ? l : [...l.slice(-40), job.message!]))
  }, [job?.message])

  const start = async () => {
    if (!fid) return
    try {
      setLog([])
      const j = await api.post<Job>(`/api/farms/${fid}/perfect`, { seeds, members, quick, target })
      setJobId(j.id)
      toast(j.target === 'remote' ? 'Sent to the GPU server' : 'Iterations started on this computer')
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }

  const usePlan = async (plan: string) => {
    if (!fid || !resJob) return
    try {
      await api.post<Plan>(`/api/farms/${fid}/plans`, { job_id: resJob.id, plan })
      go(href.farm(fid, 'plan'))
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }

  if (!farm) return null
  const gpu = workers?.remote.find((w) => w.alive && w.kinds.includes('perfect'))
  const cal = res?.ranked.find((r) => r.plan === 'calendar')
  const runBar = (
    <section className="sheet sheet-pad" style={{ marginBottom: 20 }} aria-labelledby="run-h">
      <div className="section-head" style={{ marginBottom: 10 }}>
        <h2 id="run-h">Run the iterations</h2>
        <span className="xs muted">{gpu ? `GPU server connected: ${gpu.name}` : 'No GPU server connected — connect the H200 from System.'}</span>
      </div>
      <div className="run-bar">
        <Field label="Seasons per plan" hint="Different weed and blight draws.">
          <input className="input num" type="number" min={1} max={10} value={seeds} onChange={(e) => setSeeds(Number(e.target.value))} />
        </Field>
        <Field label="Forecast members" hint="Ensemble members per decision.">
          <input className="input num" type="number" min={2} max={30} value={members} onChange={(e) => setMembers(Number(e.target.value))} />
        </Field>
        <div className="field">
          <span>Where to compute</span>
          <Segmented label="Where to compute" value={target} onChange={setTarget}
            options={[{ value: 'auto', label: 'Automatic' }, { value: 'local', label: 'This computer' }, { value: 'remote', label: 'GPU server' }]} />
          <small>&nbsp;</small>
        </div>
        <label className="check" style={{ alignSelf: 'center' }}><input type="checkbox" checked={quick} onChange={(e) => setQuick(e.target.checked)} />Quick flight search (16 settings, not 240)</label>
        <button className="btn btn-primary btn-l" style={{ alignSelf: 'center' }} disabled={!!jobId || (target === 'remote' && !gpu)} onClick={start}>
          <Icon name="flask" />{jobId ? 'Iterations running' : 'Run the iterations'}
        </button>
      </div>
      {job ? <div style={{ marginTop: 14 }}><JobBar job={job} /></div> : null}
      {log.length ? <ul className="xs muted num" style={{ paddingLeft: 16, marginTop: 8, maxHeight: 160, overflowY: 'auto' }}>{log.map((l, i) => <li key={i}>{l}</li>)}</ul> : null}
    </section>
  )

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Iterations</h1>
          <p className="muted">The twin rehearses the whole season many times, with the forecasts that were really issued, and ranks plans
            and K3-A Pro flight settings for this field. {farm.mode === 'field' ? 'A live field rehearses on its own previous season.' : ''}</p>
        </div>
      </div>
      {runBar}
      {res ? (
        <>
          <div className="stats">
            <div className="stat"><b>{planName(res.best.plan)}</b><span>Best plan</span><small>{res.rehearsal.kind}, {res.rehearsal.year}</small></div>
            <div className="stat"><b>{fmt.money(res.best.net_mean, res.currency)}</b><span>Expected net per ha</span>
              {cal ? <small>{res.best.net_mean - cal.net_mean >= 0 ? '+' : ''}{fmt.money(res.best.net_mean - cal.net_mean, res.currency)} vs calendar</small> : null}</div>
            <div className="stat"><b>{fmt.money(res.best.net_cvar20, res.currency)}</b><span>Bad-case net (worst 20%)</span></div>
            {res.timings_s ? <div className="stat"><b>{fmt.minutes((res.timings_s.stage1 + res.timings_s.stage2) / 60)}</b><span>Compute time</span>
              <small>flights {fmt.n(res.timings_s.stage1, 0)} s · seasons {fmt.n(res.timings_s.stage2, 0)} s</small></div> : null}
          </div>
          <section className="section">
            <div className="section-head"><h2>Ranked plans</h2>
              <span className="xs muted">Score = expected net − ½ × (expected − worst 20%) · {fmt.count(res.ranked[0]?.n_seasons, 'season')} each</span></div>
            <div className="table-wrap"><table className="table">
              <thead><tr><th>Plan</th><th className="n">Expected net</th><th className="n">Worst 20%</th><th className="n">Score</th><th className="n">Yield (kg DM/ha)</th>
                <th className="n">Sprays</th><th className="n">Litres</th><th className="n">Drone min</th><th className="n">CV %</th><th></th></tr></thead>
              <tbody>{res.ranked.map((r, i) => (
                <tr key={r.plan} className={i === 0 ? 'best' : undefined}>
                  <td className="plan-col"><b>{planName(r.plan)}</b><div className="xs muted">{r.policy} policy · scan day {r.scan_das} at {(r.scan_gsd_m * 1000).toFixed(0)} mm</div></td>
                  <td className="n">{fmt.n(r.net_mean, 0)}</td><td className="n">{fmt.n(r.net_cvar20, 0)}</td><td className="n">{fmt.n(r.score, 0)}</td>
                  <td className="n">{fmt.n(r.yield_dry_mean, 0)}</td><td className="n">{fmt.n(r.fungicide_sprays_mean, 1)}</td>
                  <td className="n">{fmt.n(r.litres_mean, 1)}</td><td className="n">{fmt.n(r.drone_time_min_mean, 0)}</td><td className="n">{fmt.n(r.cv_pct_mean, 0)}</td>
                  <td style={{ whiteSpace: 'nowrap' }}><button className={`btn btn-s${i === 0 ? ' btn-primary' : ''}`} onClick={() => usePlan(r.plan)}>Use this plan</button></td>
                </tr>
              ))}</tbody>
            </table></div>
            <p className="xs faint" style={{ marginTop: 6 }}>Money in {res.currency} per hectare; prices come from the region pack and are illustrative until set from local market data.</p>
          </section>
          <div className="cols-main">
            <section className="section">
              <div className="section-head"><h2>K3-A Pro flight settings</h2><span className="xs muted">{res.flight_ranking[0]?.scale}</span></div>
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Height</th><th>Speed</th><th>Row spacing</th><th>Rate</th><th>RTK</th><th className="n">Score /ha</th><th className="n">ha/h</th><th className="n">CV %</th><th className="n">Under-dosed</th></tr></thead>
                <tbody>{res.flight_ranking.map((f, i) => (
                  <tr key={i} className={i === 0 ? 'best' : undefined}>
                    <td>{f.settings.height_m} m</td><td>{f.settings.speed_ms} m/s</td><td>{f.settings.swath_m} m</td><td>{f.settings.rate_l_ha} L/ha</td>
                    <td>{f.settings.rtk ? 'yes' : 'no'}</td><td className="n">{fmt.n(f.score, 0)}</td><td className="n">{fmt.n(f.kpis_mean.ha_per_hour, 2)}</td>
                    <td className="n">{fmt.n(f.kpis_mean.cv_pct, 1)}</td><td className="n">{fmt.pct(f.kpis_mean.skipped_frac, 1)}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            </section>
            <div>
              {res.logistics.length ? (
                <section className="section">
                  <h2 style={{ marginBottom: 10 }}>Batteries and chargers</h2>
                  <div className="table-wrap"><table className="table">
                    <thead><tr><th>Battery sets</th><th>Chargers</th><th className="n">Job time</th><th className="n">ha/h</th><th className="n">Swaps</th></tr></thead>
                    <tbody>{res.logistics.map((l) => (
                      <tr key={`${l.battery_sets}-${l.chargers}`}><td>{l.battery_sets}</td><td>{l.chargers}</td><td className="n">{fmt.minutes(l.time_min)}</td>
                        <td className="n">{fmt.n(l.ha_per_hour, 2)}</td><td className="n">{fmt.n(l.battery_swaps, 1)}</td></tr>
                    ))}</tbody>
                  </table></div>
                </section>
              ) : null}
              {res.warnings.length ? (
                <section className="section">
                  <h3 style={{ marginBottom: 6 }}>Read before trusting the numbers</h3>
                  <ul className="small muted" style={{ paddingLeft: 18, margin: 0 }}>{res.warnings.map((w) => <li key={w}>{w}</li>)}</ul>
                </section>
              ) : null}
            </div>
          </div>
        </>
      ) : (
        <div className="sheet"><Empty title="No iterations yet">Run them to rank plans and flight settings. A small field takes a few minutes on this computer;
          large fields are faster on a GPU server.</Empty></div>
      )}
      <section className="section">
        <h3 style={{ marginBottom: 8 }}>Earlier runs</h3>
        {jobs.length ? (
          <div className="table-wrap"><table className="table"><tbody>{jobs.slice(0, 8).map((j) => (
            <tr key={j.id}><td className="num" style={{ whiteSpace: 'nowrap' }}>{fmt.time(j.created_at)}</td><td><Badge kind={STATUS_KIND[j.status]}>{j.status}</Badge></td>
              <td className="small">{j.worker && !j.worker.startsWith('local') ? `on ${j.worker}` : j.target === 'remote' ? 'GPU server' : 'this computer'}</td>
              <td className="small">{j.result ? `Best: ${planName(String(j.result.best))}` : j.error ? <span style={{ color: 'var(--alarm)' }}>{j.error.slice(0, 80)}</span> : j.message}</td></tr>
          ))}</tbody></table></div>
        ) : <p className="small muted">None yet.</p>}
      </section>
    </>
  )
}
