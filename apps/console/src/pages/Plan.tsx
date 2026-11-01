import { useCallback, useEffect, useState } from 'react'
import { api, ApiError, type Plan } from '../api'
import { useApp, useFarm } from '../state'
import { href } from '../router'
import { Icon } from '../ui/icons'
import { Badge, Dialog, Empty, fmt, Notice, STATUS_KIND } from '../ui/core'
import { planName } from './planNames'

const OP_NAMES: Record<string, string> = { fungicide: 'Fungicide spray', herbicide: 'Herbicide spray', scan: 'Camera-drone scan' }

export default function PlanPage() {
  const { farm, refresh } = useFarm()
  const { toast } = useApp()
  const [plans, setPlans] = useState<Plan[] | null>(null)
  const [sel, setSel] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<string | null>(null)
  const fid = farm?.id

  const load = useCallback(async () => {
    if (!fid) return
    const r = await api.get<{ plans: Plan[] }>(`/api/farms/${fid}/plans`)
    setPlans(r.plans)
    setSel((s) => s ?? (r.plans.find((p) => p.status === 'draft') ?? r.plans.find((p) => p.status === 'approved') ?? r.plans[0])?.id ?? null)
  }, [fid])
  useEffect(() => { load().catch((e) => toast(e.message, 'bad')) }, [load, toast])

  const approve = async (pid: string, confirm_reset = false) => {
    try {
      await api.post(`/api/plans/${pid}/approve`, { confirm_reset })
      setConfirm(null)
      toast('Plan approved: the mission now runs this plan')
      await load()
      await refresh()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConfirm(pid)
      else toast((e as Error).message, 'bad')
    }
  }

  if (!farm || plans === null) return null
  if (!plans.length) {
    return (
      <>
        <div className="page-head"><div><h1>Plan</h1></div></div>
        <div className="sheet"><Empty title="No plan yet" action={<a className="btn btn-primary" href={href.farm(farm.id, 'iterate')}>Open the iterations</a>}>
          Choose a plan from the iterations. Until one is approved, the mission runs the twin with the default flight settings.
        </Empty></div>
      </>
    )
  }
  const plan = plans.find((p) => p.id === sel) ?? plans[0]
  const p = plan.payload
  const f = p.flight
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Plan</h1>
          <p className="muted">{planName(p.plan)} — what the season is expected to take, and the files for the K3-A Pro.</p>
        </div>
        <div className="btn-row">
          <select className="input" style={{ width: 'auto' }} aria-label="Plan version" value={plan.id} onChange={(e) => setSel(e.target.value)}>
            {plans.map((x) => <option key={x.id} value={x.id}>{planName(x.payload.plan)} ({x.status}, {fmt.short(x.created_at)})</option>)}
          </select>
          {plan.status === 'approved' ? <Badge kind="ok">Approved</Badge> : (
            <button className="btn btn-primary" onClick={() => approve(plan.id)}><Icon name="check" />Approve this plan</button>
          )}
        </div>
      </div>

      <div className="stats" style={{ marginBottom: 18 }}>
        <div className="stat"><b>{fmt.money(p.net_mean, p.currency)}</b><span>Expected net per ha</span><small>worst 20%: {fmt.money(p.cvar20, p.currency)}</small></div>
        <div className="stat"><b>{fmt.n(p.totals.missions, 1)}</b><span>Spray missions expected</span><small>{fmt.count(p.totals.scans, 'scan')}</small></div>
        <div className="stat"><b>{fmt.n(p.totals.drone_h, 1)} h</b><span>Drone time</span><small>crew {fmt.n(p.totals.crew_h, 1)} h</small></div>
        <div className="stat"><b>{fmt.n(p.totals.litres, 0)} L</b><span>Spray mix</span><small>whole season</small></div>
        <div className="stat"><b>{fmt.n(p.yield_dry_mean, 0)}</b><span>Yield (kg dry matter/ha)</span><small>{fmt.count(p.n_seasons, 'rehearsed season', 'rehearsed seasons', 0)}</small></div>
      </div>

      <div className="cols-main">
        <div>
          <section className="section" style={{ paddingTop: 0 }}>
            <div className="section-head"><h2>Season schedule</h2><span className="xs muted">Sown {fmt.date(p.season.sowing)}, harvest {fmt.date(p.season.harvest)}</span></div>
            <div className="table-wrap"><table className="table">
              <thead><tr><th>Operation</th><th className="n">Expected</th><th>Window</th><th className="n">Drone</th><th className="n">Crew</th><th className="n">Sorties</th><th className="n">Swaps</th><th className="n">Litres</th></tr></thead>
              <tbody>{p.operations.map((o) => (
                <tr key={o.product}>
                  <td><b>{OP_NAMES[o.product] ?? o.product}</b>
                    <div className="xs muted">{o.product === 'scan' ? `${((o.gsd_m ?? 0) * 1000).toFixed(0)} mm resolution` : `day ${o.das_first}–${o.das_last} after sowing`}</div></td>
                  <td className="n">{fmt.n(o.count_mean, 1)}×</td>
                  <td>{o.date_first === o.date_last ? fmt.date(o.date_first) : `${fmt.short(o.date_first)} – ${fmt.date(o.date_last)}`}</td>
                  <td className="n">{o.time_min !== undefined ? fmt.minutes(o.time_min) : '–'}</td><td className="n">{o.crew_min !== undefined ? fmt.minutes(o.crew_min) : '–'}</td>
                  <td className="n">{o.sorties ?? '–'}</td><td className="n">{o.battery_swaps ?? '–'}</td><td className="n">{o.litres ?? '–'}</td>
                </tr>
              ))}</tbody>
            </table></div>
            <p className="xs faint" style={{ marginTop: 6 }}>Times are per mission. The twin decides each spray during the season; windows show when they fell in the rehearsals.</p>
          </section>
          {p.logistics.length ? (
            <section className="section">
              <h2 style={{ marginBottom: 8 }}>Batteries and chargers</h2>
              {p.logistics_pick ? <Notice kind="ok">Recommended: <b>{p.logistics_pick.battery_sets} battery sets and {p.logistics_pick.chargers} charger{p.logistics_pick.chargers > 1 ? 's' : ''}</b> — {p.logistics_pick.why}.</Notice> : null}
              <div className="table-wrap" style={{ marginTop: 10 }}><table className="table">
                <thead><tr><th>Battery sets</th><th>Chargers</th><th className="n">Job time</th><th className="n">ha/h</th></tr></thead>
                <tbody>{p.logistics.map((l) => <tr key={`${l.battery_sets}-${l.chargers}`}><td>{l.battery_sets}</td><td>{l.chargers}</td><td className="n">{fmt.minutes(l.time_min)}</td><td className="n">{fmt.n(l.ha_per_hour, 2)}</td></tr>)}</tbody>
              </table></div>
            </section>
          ) : null}
          <section className="section">
            <h3 style={{ marginBottom: 6 }}>Warnings and assumptions</h3>
            <ul className="small muted" style={{ paddingLeft: 18, margin: 0 }}>{[...p.warnings, ...p.assumptions].map((w) => <li key={w}>{w}</li>)}</ul>
          </section>
        </div>
        <aside>
          <section className="section" style={{ paddingTop: 0 }}>
            <h3 style={{ marginBottom: 8 }}>Enter in Agri Assistant (K3-A Pro)</h3>
            <table className="table"><tbody>
              <tr><td>Work height</td><td className="n">{f.height_m} m</td></tr>
              <tr><td>Work speed</td><td className="n">{f.speed_ms} m/s</td></tr>
              <tr><td>Row spacing</td><td className="n">{f.swath_m} m</td></tr>
              <tr><td>Dose</td><td className="n">{f.rate_l_ha} L/ha ({(f.rate_l_ha * 666.67 / 10000).toFixed(2)} L/mu)</td></tr>
              <tr><td>Flow mode</td><td className="n">linkage</td></tr>
              <tr><td>RTK</td><td className="n">{f.rtk ? 'on' : 'off'}</td></tr>
            </tbody></table>
            <div className="btn-row" style={{ marginTop: 10 }}>
              <a className="btn" href={`/api/farms/${farm.id}/export/kml`}><Icon name="download" size={18} />Field KML</a>
              <a className="btn" href={`/api/farms/${farm.id}/export/settings`} target="_blank" rel="noreferrer">Settings sheet</a>
              <a className="btn" href={`/api/farms/${farm.id}/export/survey.kml?gsd_m=${p.scan.gsd_m}`}><Icon name="download" size={18} />Survey KML</a>
            </div>
          </section>
          <section className="section">
            <h3 style={{ marginBottom: 8 }}>Take the plan with you</h3>
            <div className="btn-row">
              <a className="btn" href={`/api/farms/${farm.id}/export/plan.pdf`}><Icon name="download" size={18} />PDF</a>
              <a className="btn" href={`/api/farms/${farm.id}/export/plan.ics`}>Calendar</a>
              <a className="btn" href={`/api/farms/${farm.id}/export/plan.csv`}>CSV</a>
            </div>
            <p className="xs faint" style={{ marginTop: 6 }}>Exports follow the approved plan.</p>
          </section>
          <section className="section">
            <h3 style={{ marginBottom: 6 }}>Versions</h3>
            <table className="table"><tbody>{plans.map((x) => (
              <tr key={x.id}><td>{planName(x.payload.plan)}</td><td><Badge kind={STATUS_KIND[x.status]}>{x.status}</Badge></td><td className="num xs">{fmt.short(x.created_at)}</td></tr>
            ))}</tbody></table>
          </section>
        </aside>
      </div>
      {confirm ? (
        <Dialog title="Restart the mission with this plan?" onClose={() => setConfirm(null)}
          actions={<><button className="btn" onClick={() => setConfirm(null)}>Keep the current mission</button><button className="btn btn-primary" onClick={() => approve(confirm, true)}>Approve and restart</button></>}>
          <p>The mission has already started. Approving this plan restarts it from day 1 with the new policy, flight settings and scan schedule. Records already imported stay.</p>
        </Dialog>
      ) : null}
    </>
  )
}
