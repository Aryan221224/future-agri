// Live data for one farm: alerts, the spray outlook, observed weather, the ensemble
// forecast, Sentinel-2 and field devices. Every feed degrades on its own.
import { useCallback, useEffect, useState } from 'react'
import { api, type Alert, type Device, type Outlook, type Reading, type SatelliteFeed, type WeatherFeed } from '../api'
import { LineChart } from '../charts/LineChart'
import { useApp, useFarm } from '../state'
import { Icon } from '../ui/icons'
import { Badge, Dialog, Empty, Field, fmt, Notice } from '../ui/core'

const DEVICE_KINDS = [
  { value: 'soil_moisture', label: 'Soil-moisture probe' },
  { value: 'weather_station', label: 'Weather station' },
  { value: 'tracker', label: 'GPS tracker on the drone' },
  { value: 'camera', label: 'Field camera' },
]
const kindLabel = (k: string) => DEVICE_KINDS.find((d) => d.value === k)?.label ?? k

function Unavailable({ what, reason, onRetry }: { what: string; reason?: string; onRetry: () => void }) {
  return (
    <Notice kind="warn">
      <b>{what} unavailable</b>{reason ? <> — {reason}</> : null}{' '}
      <button className="btn btn-s" style={{ marginLeft: 6 }} onClick={onRetry}><Icon name="refresh" size={16} />Try again</button>
    </Notice>
  )
}

function Stale({ feed }: { feed: { stale?: boolean; stale_reason?: string; fetched?: string } | null }) {
  if (!feed?.stale) return null
  return <Notice kind="warn">Showing data from {fmt.time(feed.fetched)}; the latest update failed{feed.stale_reason ? ` (${feed.stale_reason})` : ''}.</Notice>
}

export default function Live() {
  const { farm } = useFarm()
  const { toast } = useApp()
  const [alerts, setAlerts] = useState<Alert[] | null>(null)
  const [outlook, setOutlook] = useState<Outlook | null>(null)
  const [weather, setWeather] = useState<WeatherFeed | null>(null)
  const [sat, setSat] = useState<SatelliteFeed | null>(null)
  const [devices, setDevices] = useState<Device[]>([])
  const [readings, setReadings] = useState<Reading[]>([])
  const [adding, setAdding] = useState(false)
  const fid = farm?.id

  const loadFeeds = useCallback(() => {
    if (!fid) return
    const err = (e: Error) => ({ available: false, reason: e.message })
    api.get<{ alerts: Alert[] }>(`/api/farms/${fid}/live/alerts`).then((r) => setAlerts(r.alerts)).catch(() => setAlerts([]))
    api.get<Outlook>(`/api/farms/${fid}/live/outlook`).then(setOutlook).catch((e) => setOutlook(err(e) as Outlook))
    api.get<WeatherFeed>(`/api/farms/${fid}/live/weather`).then(setWeather).catch((e) => setWeather(err(e) as WeatherFeed))
    api.get<SatelliteFeed>(`/api/farms/${fid}/live/satellite`).then(setSat).catch((e) => setSat(err(e) as SatelliteFeed))
  }, [fid])
  const loadDevices = useCallback(async () => {
    if (!fid) return
    try {
      const [d, r] = await Promise.all([
        api.get<{ devices: Device[] }>(`/api/farms/${fid}/devices`),
        api.get<{ readings: Reading[] }>(`/api/farms/${fid}/readings?limit=200`),
      ])
      setDevices(d.devices)
      setReadings(r.readings)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }, [fid, toast])
  useEffect(() => { loadFeeds(); loadDevices() }, [loadFeeds, loadDevices])
  useEffect(() => {
    const h = window.setInterval(loadDevices, 30_000)
    return () => window.clearInterval(h)
  }, [loadDevices])

  const remove = async (d: Device) => {
    if (!fid) return
    try {
      await api.del(`/api/farms/${fid}/devices/${d.id}`)
      await loadDevices()
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }

  if (!farm) return null
  const obs = weather?.available ? weather.observed : []
  const fc = weather?.available ? weather.forecast : null
  const latest = (d: Device) => readings.filter((r) => r.device_id === d.id).sort((a, b) => (a.ts < b.ts ? 1 : -1))[0]
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Live</h1>
          <p className="muted">Weather, the spray outlook, satellite and field devices for {farm.name}.
            {farm.mode === 'sim' ? ' A virtual farm shows what was known on the mission day.' : ''}</p>
        </div>
        <button className="btn" onClick={() => { loadFeeds(); loadDevices() }}><Icon name="refresh" size={18} />Refresh</button>
      </div>

      {alerts?.length ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 18 }}>
          {alerts.map((a, i) => <Notice key={i} kind={a.level === 'alert' ? 'bad' : a.level === 'warn' ? 'warn' : ''}>{a.text}</Notice>)}
        </div>
      ) : null}

      <section className="section" style={{ paddingTop: 0 }} aria-labelledby="outlook-h">
        <div className="section-head">
          <h2 id="outlook-h">Spray outlook</h2>
          {outlook?.available ? <span className="xs muted">{outlook.source}, issued {fmt.time(outlook.issue)}</span> : null}
        </div>
        {outlook === null ? <p className="small muted">Loading…</p> : !outlook.available ? (
          <Unavailable what="Spray outlook" reason={outlook.reason} onRetry={loadFeeds} />
        ) : (
          <>
            <Stale feed={outlook} />
            <div className="days" style={{ marginTop: 8 }}>
              {outlook.days.map((d) => {
                const pct = Math.round(d.p_flyable * 100)
                return (
                  <div key={d.day} className={`day${pct < 25 ? ' bad' : pct < 50 ? ' poor' : ''}`}>
                    <b>{fmt.short(d.day)}</b>
                    <div className="meter" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
                    <span>{pct}%</span>
                    <small>wind {d.wind_dawn_p50.toFixed(1)} m/s</small>
                    <small>rain {d.rain_p50.toFixed(1)} mm</small>
                  </div>
                )
              })}
            </div>
            <p className="xs faint" style={{ marginTop: 8 }}>Share of forecast members in which flying is allowed: dawn wind ≤ {outlook.limits.wind_ms} m/s,
              rain ≤ {outlook.limits.rain_mm} mm, max temperature ≤ {outlook.limits.tmax_c} °C. {outlook.note}.</p>
          </>
        )}
      </section>

      <div className="cols-main">
        <div>
          <section className="section" aria-labelledby="obs-h">
            <div className="section-head"><h2 id="obs-h">Observed weather</h2>{weather?.available ? <span className="xs muted">{weather.note}</span> : null}</div>
            {weather === null ? <p className="small muted">Loading…</p> : !weather.available ? (
              <Unavailable what="Weather" reason={weather.reason} onRetry={loadFeeds} />
            ) : obs.length ? (
              <>
                <Stale feed={weather} />
                <LineChart labels={obs.map((o) => o.day)} yTitle="°C" ariaLabel="Observed daily temperature, last 30 days"
                  series={[{ label: 'Max', data: obs.map((o) => o.tmax), slot: 2 }, { label: 'Min', data: obs.map((o) => o.tmin), slot: 0 }]} height={190} />
                <LineChart labels={obs.map((o) => o.day)} yTitle="mm / m/s" yMin={0} ariaLabel="Observed daily rain and wind, last 30 days"
                  series={[{ label: 'Rain (mm)', data: obs.map((o) => o.rain_mm), slot: 0, kind: 'bar' }, { label: 'Wind (m/s)', data: obs.map((o) => o.wind_ms), slot: 3 }]} height={170} />
              </>
            ) : <p className="small muted">No observed days yet — the season starts on {fmt.date(farm.sowing)}.</p>}
          </section>

          <section className="section" aria-labelledby="fc-h">
            <div className="section-head"><h2 id="fc-h">Forecast</h2>{fc?.source ? <span className="xs muted">{fc.source}, {fc.members} members, issued {fmt.time(fc.issue)}</span> : null}</div>
            {fc && fc.days.length ? (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Day</th><th className="n">Max °C</th><th className="n">Min °C</th><th className="n">Rain mm</th><th className="n">Wind m/s</th></tr></thead>
                <tbody>{fc.days.map((d) => (
                  <tr key={d.day}>
                    <td>{fmt.short(d.day)}</td>
                    {[d.tmax, d.tmin, d.rain_mm, d.wind_ms].map((v, i) => (
                      <td key={i} className="n">{v[1].toFixed(1)} <span className="xs faint">({v[0].toFixed(1)}–{v[2].toFixed(1)})</span></td>
                    ))}
                  </tr>
                ))}</tbody>
              </table></div>
            ) : <p className="small muted">{fc?.reason ? `No forecast: ${fc.reason}` : weather?.available === false ? 'No forecast while the weather feed is down.' : 'Loading…'}</p>}
            {fc?.days.length ? <p className="xs faint" style={{ marginTop: 6 }}>Median of the ensemble, with the 10–90% range in brackets.</p> : null}
          </section>
        </div>

        <aside>
          <section className="section" aria-labelledby="sat-h">
            <div className="section-head"><h2 id="sat-h">Satellite</h2>{sat?.source ? <span className="xs muted">{sat.source}</span> : null}</div>
            {sat === null ? <p className="small muted">Loading…</p> : !sat.available ? (
              <Unavailable what="Satellite" reason={sat.reason} onRetry={loadFeeds} />
            ) : (
              <>
                {sat.latest.available && sat.latest.url ? (
                  <figure style={{ margin: 0 }}>
                    <img src={sat.latest.url} alt={`Sentinel-2 true colour image of ${farm.name}`} style={{ width: '100%', borderRadius: 'var(--radius-s)', border: '1px solid var(--rule)' }} />
                    <figcaption className="xs muted" style={{ marginTop: 4 }}>{fmt.date(sat.latest.date)}{sat.latest.cloud !== undefined ? `, ${Math.round(sat.latest.cloud)}% cloud` : ''}</figcaption>
                  </figure>
                ) : <p className="small muted">No cloud-free Sentinel-2 image of this field yet.</p>}
                {sat.series.length ? (
                  <LineChart labels={sat.series.map((s) => s.date)} yTitle="NDVI" yMin={0} height={170} ariaLabel="Sentinel-2 NDVI of the field"
                    series={[{ label: 'NDVI', data: sat.series.map((s) => s.ndvi), slot: 1 }]} />
                ) : <p className="xs faint" style={{ marginTop: 6 }}>NDVI appears after the first clear pass of the season.</p>}
              </>
            )}
          </section>

          <section className="section" aria-labelledby="dev-h">
            <div className="section-head">
              <h2 id="dev-h">Field devices</h2>
              <button className="btn btn-s" onClick={() => setAdding(true)}><Icon name="plus" size={16} />Add a device</button>
            </div>
            {devices.length ? (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Device</th><th>Last reading</th><th></th></tr></thead>
                <tbody>{devices.map((d) => {
                  const r = latest(d)
                  const silent = !d.last_seen || Date.now() - new Date(d.last_seen).getTime() > 86400_000
                  return (
                    <tr key={d.id}>
                      <td><b>{d.name}</b><div className="xs muted">{kindLabel(d.kind)}</div></td>
                      <td className="small">
                        {r ? <>{r.value !== null ? `${fmt.n(r.value, 1)} ${r.unit ?? ''}` : r.kind} <span className="xs faint">{fmt.time(r.ts)}</span></> : <span className="muted">no data yet</span>}
                        {silent && d.last_seen ? <div><Badge kind="warn">silent for a day</Badge></div> : null}
                      </td>
                      <td><button className="btn btn-quiet btn-s" aria-label={`Remove ${d.name}`} onClick={() => remove(d)}><Icon name="trash" size={16} /></button></td>
                    </tr>
                  )
                })}</tbody>
              </table></div>
            ) : <Empty title="No devices">Soil probes, weather stations and a GPS tracker on the drone can send readings to this console.</Empty>}
          </section>
        </aside>
      </div>

      {adding ? <AddDevice fid={farm.id} onClose={() => { setAdding(false); loadDevices() }} /> : null}
    </>
  )
}

function AddDevice({ fid, onClose }: { fid: string; onClose: () => void }) {
  const { toast } = useApp()
  const [name, setName] = useState('')
  const [kind, setKind] = useState('soil_moisture')
  const [token, setToken] = useState<string | null>(null)
  const create = async () => {
    try {
      const r = await api.post<{ token: string }>(`/api/farms/${fid}/devices`, { kind, name: name.trim() })
      setToken(r.token)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  const origin = location.origin
  if (token) {
    const sample = kind === 'tracker'
      ? `{"token":"${token}","readings":[{"kind":"position","ts":"2026-06-01T06:10:00Z","payload":{"lat":0,"lon":0,"alt_m":2.5,"spraying":true}}]}`
      : `{"token":"${token}","readings":[{"kind":"${kind === 'weather_station' ? 'air_temp' : 'soil_moisture'}","ts":"2026-06-01T06:00:00Z","value":23.5}]}`
    return (
      <Dialog title="Device created" onClose={onClose} actions={<button className="btn btn-primary" onClick={onClose}>Done</button>}>
        <Notice kind="warn"><b>Copy this token now</b> — it is stored only as a hash and is not shown again.</Notice>
        <pre className="code" style={{ marginTop: 10, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{token}</pre>
        <p className="small muted" style={{ marginTop: 10 }}>The device sends readings with an HTTP POST to <code>{origin}/api/ingest</code>:</p>
        <pre className="code xs" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{sample}</pre>
        <p className="xs faint">Or test it with the emulator: <code>python -m fa_console.emulate --server {origin} --token …</code></p>
      </Dialog>
    )
  }
  return (
    <Dialog title="Add a field device" onClose={onClose}
      actions={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={!name.trim()} onClick={create}>Create device</button></>}>
      <div className="grid-form" style={{ gridTemplateColumns: '1fr' }}>
        <Field label="Device name"><input className="input" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} placeholder="Probe north corner" /></Field>
        <Field label="Kind">
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
            {DEVICE_KINDS.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
          </select>
        </Field>
      </div>
    </Dialog>
  )
}
