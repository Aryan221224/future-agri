import { useEffect, useMemo, useState } from 'react'
import { api, type SessionEvent, type TraceRow } from '../api'
import { useFarm } from '../state'
import { href } from '../router'
import { Empty, fmt } from '../ui/core'
import { LineChart, type Marker, type Series } from '../charts/LineChart'

export default function Season() {
  const { farm, session } = useFarm()
  const [trace, setTrace] = useState<TraceRow[] | null>(null)
  const [events, setEvents] = useState<SessionEvent[]>([])
  const fid = farm?.id
  useEffect(() => {
    if (!fid) return
    api.get<{ trace: TraceRow[]; events: SessionEvent[] }>(`/api/farms/${fid}/session/trace`)
      .then((r) => { setTrace(r.trace); setEvents(r.events) }).catch(() => setTrace([]))
  }, [fid, session?.n_trace])

  const d = useMemo(() => {
    const tr = trace ?? []
    const labels = tr.map((r) => r.day)
    const markers: Marker[] = events.filter((e) => e.kind === 'flight' || e.kind === 'scan').map((e) => ({ at: e.day, kind: e.kind as 'flight' | 'scan' }))
    const col = (k: keyof TraceRow) => tr.map((r) => (r[k] === null || r[k] === undefined ? null : Number(r[k])))
    return { labels, markers, col, hasTruth: tr.some((r) => r.truth_lai !== null) }
  }, [trace, events])

  if (!farm) return null
  if (trace === null) return null
  if (!trace.length) {
    return (
      <>
        <div className="page-head"><div><h1>Season</h1></div></div>
        <div className="sheet"><Empty title="No season yet" action={<a className="btn btn-primary" href={href.farm(farm.id, 'mission')}>Open the mission</a>}>
          Charts fill in as the mission runs: the crop the twin sees, the hidden truth on virtual farms, weather and every decision.
        </Empty></div>
      </>
    )
  }
  const lai: Series[] = [{ label: 'Twin estimate', data: d.col('twin_lai'), slot: 0 }]
  if (d.hasTruth) lai.push({ label: 'Ground truth', data: d.col('truth_lai'), slot: 1, dashed: true })
  const plans = events.filter((e) => e.kind === 'plan')
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Season</h1>
          <p className="muted">{fmt.date(trace[0].day)} to {fmt.date(trace[trace.length - 1].day)}: what the twin saw and decided.
            {d.hasTruth ? ' On a virtual farm the hidden ground truth is shown for comparison.' : ''}</p>
        </div>
      </div>
      <p className="xs muted" style={{ marginBottom: 10 }}><span style={{ color: 'var(--survey)' }}>|</span> vertical lines: flights <span style={{ color: 'var(--sky)', marginLeft: 8 }}>|</span> scans</p>
      <div className="chart-grid">
        <section>
          <h3 style={{ marginBottom: 4 }}>Leaf area index</h3>
          <LineChart labels={d.labels} series={lai} yTitle="m² leaf per m² ground" markers={d.markers} yMin={0} ariaLabel="Leaf area index over the season" />
        </section>
        {d.hasTruth ? (
          <section>
            <h3 style={{ marginBottom: 4 }}>Late blight (mean severity)</h3>
            <LineChart labels={d.labels} series={[{ label: 'Blight severity', data: d.col('truth_blight').map((v) => (v === null ? null : v * 100)), slot: 3 }]}
              yTitle="% of leaf area" markers={d.markers} yMin={0} ariaLabel="Late blight severity over the season" />
          </section>
        ) : null}
        {d.hasTruth ? (
          <section>
            <h3 style={{ marginBottom: 4 }}>Weeds (mean density)</h3>
            <LineChart labels={d.labels} series={[{ label: 'Weeds', data: d.col('truth_weeds_m2'), slot: 2 }]} yTitle="plants per m²" markers={d.markers} yMin={0} ariaLabel="Weed density over the season" />
          </section>
        ) : null}
        <section>
          <h3 style={{ marginBottom: 4 }}>Temperature</h3>
          <LineChart labels={d.labels} series={[{ label: 'Max', data: d.col('tmax'), slot: 0 }, { label: 'Min', data: d.col('tmin'), slot: 1 }]} yTitle="°C" markers={d.markers} ariaLabel="Daily maximum and minimum temperature" />
        </section>
        <section>
          <h3 style={{ marginBottom: 4 }}>Rain</h3>
          <LineChart labels={d.labels} series={[{ label: 'Rain', data: d.col('rain_mm'), slot: 0, kind: 'bar' }]} yTitle="mm per day" yMin={0} ariaLabel="Daily rain" />
        </section>
        <section>
          <h3 style={{ marginBottom: 4 }}>Wind (2 m, daily mean)</h3>
          <LineChart labels={d.labels} series={[{ label: 'Wind', data: d.col('wind_ms'), slot: 0 }]} yTitle="m/s" yMin={0} markers={d.markers}
            limit={(session?.plan ? null : null)} ariaLabel="Daily mean wind" />
        </section>
      </div>
      <section className="section" style={{ marginTop: 24 }}>
        <div className="section-head"><h2>Twin decisions</h2><span className="xs muted">{plans.length} weekly fungicide plans</span></div>
        {plans.length ? (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Day</th><th>Decision</th><th className="n">Spray better in</th><th className="n">Expected gain</th></tr></thead>
            <tbody>{plans.slice(-30).reverse().map((e, i) => (
              <tr key={i}><td className="num">{fmt.short(e.day)}</td><td>{e.text}</td>
                <td className="n">{e.p_spray_better !== undefined ? fmt.pct(Number(e.p_spray_better)) : '–'}</td>
                <td className="n">{e.expected_gain !== undefined ? fmt.n(Number(e.expected_gain), 0) : '–'}</td></tr>
            ))}</tbody>
          </table></div>
        ) : <p className="small muted">No weekly plans yet (they start four weeks after sowing on crops whose disease is modelled).</p>}
      </section>
    </>
  )
}
