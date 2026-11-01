// The spray log: aircraft job records, hand entries and flights recorded from the
// mission, each checked against the region's record-keeping fields.
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type RecordRow } from '../api'
import { useApp, useFarm } from '../state'
import { Icon } from '../ui/icons'
import { Badge, Dialog, Empty, Field, fmt, Notice } from '../ui/core'

const PRODUCTS = ['fungicide', 'herbicide', 'insecticide', 'fertiliser', 'other']
const FIELD_NAMES: Record<string, string> = {
  date: 'date', start_time: 'start time', product: 'product', rate_l_ha: 'rate', area_ha: 'area', aircraft_id: 'aircraft',
  operator: 'operator', wind_speed_ms: 'wind speed', wind_direction_deg: 'wind direction', temperature_c: 'temperature',
  humidity_pct: 'humidity', buffer_m: 'buffer',
}
const SOURCE: Record<string, string> = { jiyi: 'aircraft file', manual: 'entered', mission: 'mission' }
const nice = (f: string) => FIELD_NAMES[f] ?? f.replace(/_/g, ' ')

interface ImportReport { added: number; report: { rows: number; errors: string[]; file: string } }

export default function Records() {
  const { farm } = useFarm()
  const { meta, toast } = useApp()
  const [rows, setRows] = useState<RecordRow[] | null>(null)
  const [adding, setAdding] = useState(false)
  const [report, setReport] = useState<ImportReport | null>(null)
  const file = useRef<HTMLInputElement>(null)
  const fid = farm?.id

  const load = useCallback(async () => {
    if (!fid) return
    try {
      setRows((await api.get<{ records: RecordRow[] }>(`/api/farms/${fid}/records`)).records)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }, [fid, toast])
  useEffect(() => { load() }, [load])

  const upload = async (f: File) => {
    if (!fid) return
    const fd = new FormData()
    fd.append('file', f)
    try {
      const r = await api.post<ImportReport>(`/api/farms/${fid}/records/import`, fd)
      setReport(r)
      toast(`${r.added} record${r.added === 1 ? '' : 's'} imported from ${f.name}`)
      await load()
    } catch (e) {
      toast((e as Error).message, 'bad')
    } finally {
      if (file.current) file.current.value = ''
    }
  }

  if (!farm || rows === null) return null
  const region = meta?.regions.find((r) => r.id === farm.region)
  const required = region?.records ?? []
  const incomplete = rows.filter((r) => r.missing.length).length
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Records</h1>
          <p className="muted">The spray log for {farm.name}. Each record is checked against the record-keeping rules of {region?.name ?? farm.region}.</p>
        </div>
        <div className="btn-row">
          <button className="btn btn-primary" onClick={() => setAdding(true)}><Icon name="plus" size={18} />Add a record</button>
          <button className="btn" onClick={() => file.current?.click()}><Icon name="upload" size={18} />Import aircraft records</button>
          <input ref={file} type="file" accept=".csv,.json,.txt" hidden aria-label="Aircraft job-record file" data-testid="records-file"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f) }} />
        </div>
      </div>

      <div className="stats" style={{ marginBottom: 18 }}>
        <div className="stat"><b>{rows.length}</b><span>Records</span></div>
        <div className="stat"><b>{fmt.n(rows.reduce((s, r) => s + (r.litres ?? 0), 0), 1)} L</b><span>Sprayed</span></div>
        <div className="stat"><b>{fmt.n(rows.reduce((s, r) => s + (r.area_ha ?? 0), 0), 2)} ha</b><span>Area covered</span></div>
        <div className="stat"><b>{incomplete}</b><span>Incomplete</span><small>{incomplete ? 'add the missing fields' : 'all complete'}</small></div>
      </div>

      {report ? (
        <Notice kind={report.report.errors.length ? 'warn' : 'ok'}>
          {report.report.file}: {report.added} of {report.report.rows} rows imported.
          {report.report.errors.length ? <> Skipped: {report.report.errors.slice(0, 5).join('; ')}</> : null}
          {' '}Aircraft files carry no product name — set it on each record if your rules need it.
        </Notice>
      ) : null}

      <section className="section" aria-labelledby="log-h">
        <div className="section-head">
          <h2 id="log-h">Spray log</h2>
          <div className="btn-row">
            <a className="btn btn-s" href={`/api/farms/${farm.id}/export/records.csv`}><Icon name="download" size={16} />CSV</a>
            <a className="btn btn-s" href={`/api/farms/${farm.id}/export/bundle`}><Icon name="download" size={16} />Everything (zip)</a>
          </div>
        </div>
        {rows.length ? (
          <div className="table-wrap"><table className="table">
            <thead><tr><th>Date</th><th>Product</th><th className="n">Litres</th><th className="n">Area (ha)</th><th className="n">Rate (L/ha)</th><th>Operator</th><th>Source</th><th>Check</th></tr></thead>
            <tbody>{[...rows].reverse().map((r) => {
              const p = r.payload as Record<string, unknown>
              const rate = typeof p.dose_l_ha === 'number' ? p.dose_l_ha : r.litres && r.area_ha ? r.litres / r.area_ha : null
              return (
                <tr key={r.id}>
                  <td className="num" style={{ whiteSpace: 'nowrap' }}>{fmt.time(r.ts)}</td>
                  <td>{r.product && r.product !== 'unknown' ? r.product : <span className="muted">not set</span>}</td>
                  <td className="n">{fmt.n(r.litres, 1)}</td><td className="n">{fmt.n(r.area_ha, 2)}</td><td className="n">{fmt.n(rate, 1)}</td>
                  <td>{(p.operator as string) || <span className="muted">–</span>}</td>
                  <td className="small">{SOURCE[r.source] ?? r.source}</td>
                  <td>{r.missing.length ? <Badge kind="warn">missing {r.missing.map(nice).join(', ')}</Badge> : <Badge kind="ok">complete</Badge>}</td>
                </tr>
              )
            })}</tbody>
          </table></div>
        ) : (
          <div className="sheet"><Empty title="No records yet">Import the job records exported from the K3-A Pro's Agri Assistant app, add a record by hand,
            or record flights from the mission.</Empty></div>
        )}
      </section>

      {required.length ? (
        <section className="section">
          <h3 style={{ marginBottom: 6 }}>What {region?.name ?? 'this region'} asks you to record</h3>
          <p className="small muted">{required.map(nice).join(' · ')}</p>
          {region && !region.verified ? <p className="xs faint" style={{ marginTop: 4 }}>This rule pack is not verified for your country — check the local requirements.</p> : null}
        </section>
      ) : null}

      {adding ? <AddRecord fid={farm.id} onClose={(saved) => { setAdding(false); if (saved) load() }} /> : null}
    </>
  )
}

function localNow(): string {
  const d = new Date()
  d.setMinutes(d.getMinutes() - d.getTimezoneOffset())
  return d.toISOString().slice(0, 16)
}

function AddRecord({ fid, onClose }: { fid: string; onClose: (saved: boolean) => void }) {
  const { settings, toast } = useApp()
  const [f, setF] = useState({
    ts: localNow(), product: 'fungicide', litres: '', area_ha: '', operator: settings.operator, aircraft_id: settings.aircraft_id,
    wind_ms: '', wind_dir_deg: '', temp_c: '', rh_pct: '', buffer_m: '', notes: '',
  })
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((x) => ({ ...x, [k]: e.target.value }))
  const num = (v: string) => (v.trim() === '' ? undefined : Number(v))
  const save = async () => {
    try {
      const r = await api.post<RecordRow>(`/api/farms/${fid}/records`, {
        ts: new Date(f.ts).toISOString(), product: f.product, litres: Number(f.litres), area_ha: Number(f.area_ha),
        operator: f.operator || undefined, aircraft_id: f.aircraft_id || undefined, wind_ms: num(f.wind_ms),
        wind_dir_deg: num(f.wind_dir_deg), temp_c: num(f.temp_c), rh_pct: num(f.rh_pct), buffer_m: num(f.buffer_m), notes: f.notes || undefined,
      })
      toast(r.missing.length ? `Saved; still missing: ${r.missing.map(nice).join(', ')}` : 'Record saved and complete')
      onClose(true)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  const ok = Number(f.litres) >= 0 && f.litres !== '' && Number(f.area_ha) > 0
  return (
    <Dialog title="Add a spray record" onClose={() => onClose(false)}
      actions={<><button className="btn" onClick={() => onClose(false)}>Cancel</button><button className="btn btn-primary" disabled={!ok} onClick={save}>Save record</button></>}>
      <div className="grid-form" style={{ gridTemplateColumns: 'repeat(2, minmax(0, 1fr))' }}>
        <Field label="Start"><input className="input" type="datetime-local" value={f.ts} onChange={set('ts')} /></Field>
        <Field label="Product">
          <select className="input" value={f.product} onChange={set('product')}>{PRODUCTS.map((p) => <option key={p} value={p}>{p}</option>)}</select>
        </Field>
        <Field label="Litres sprayed"><input className="input num" type="number" min={0} step="0.1" value={f.litres} onChange={set('litres')} /></Field>
        <Field label="Area sprayed (ha)"><input className="input num" type="number" min={0} step="0.01" value={f.area_ha} onChange={set('area_ha')} /></Field>
        <Field label="Operator"><input className="input" value={f.operator} maxLength={80} onChange={set('operator')} /></Field>
        <Field label="Aircraft"><input className="input" value={f.aircraft_id} maxLength={80} onChange={set('aircraft_id')} /></Field>
        <Field label="Wind (m/s)"><input className="input num" type="number" min={0} step="0.1" value={f.wind_ms} onChange={set('wind_ms')} /></Field>
        <Field label="Wind from (°)"><input className="input num" type="number" min={0} max={360} value={f.wind_dir_deg} onChange={set('wind_dir_deg')} /></Field>
        <Field label="Temperature (°C)"><input className="input num" type="number" step="0.5" value={f.temp_c} onChange={set('temp_c')} /></Field>
        <Field label="Humidity (%)"><input className="input num" type="number" min={0} max={100} value={f.rh_pct} onChange={set('rh_pct')} /></Field>
        <Field label="Buffer kept (m)"><input className="input num" type="number" min={0} value={f.buffer_m} onChange={set('buffer_m')} /></Field>
        <Field label="Notes"><input className="input" value={f.notes} maxLength={200} onChange={set('notes')} /></Field>
      </div>
    </Dialog>
  )
}
