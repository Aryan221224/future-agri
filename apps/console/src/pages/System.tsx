// System: the GPU server connection, operator settings, jobs, data sources and the audit log.
import { useCallback, useEffect, useState } from 'react'
import { api, type Job, type Settings } from '../api'
import { useApp } from '../state'
import { Icon } from '../ui/icons'
import { Badge, Dialog, Field, fmt, Notice, Segmented, STATUS_KIND } from '../ui/core'

interface Workers { local: number; remote: { name: string; alive: boolean; kinds: string[]; seen_s_ago: number; info: Record<string, unknown> }[] }
interface TokenInfo { token: string; port: number; tunnel_port: number; kinds: string[] }
interface Source { layer: string; source: string; licence: string; url: string }
interface AuditEvent { id: number; farm_id: string | null; ts: string; kind: string; actor: string; payload: Record<string, unknown> }

const JOB_NAMES: Record<string, string> = { build: 'Build farm', perfect: 'Iterations', season: 'Mission run', simulate: 'Flight simulation', detect: 'Weed detection' }

export default function System() {
  const { meta, settings, saveSettings, farms, toast } = useApp()
  const [workers, setWorkers] = useState<Workers | null>(null)
  const [tok, setTok] = useState<TokenInfo | null>(null)
  const [show, setShow] = useState(false)
  const [rotate, setRotate] = useState(false)
  const [jobs, setJobs] = useState<Job[]>([])
  const [sources, setSources] = useState<{ sources: Source[]; attribution: string } | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [form, setForm] = useState<Pick<Settings, 'operator' | 'aircraft_id' | 'default_region'>>({ operator: '', aircraft_id: '', default_region: '' })

  const loadWorkers = useCallback(() => {
    api.get<Workers>('/api/workers').then(setWorkers).catch(() => undefined)
    api.get<{ jobs: Job[] }>('/api/jobs?limit=30').then((r) => setJobs(r.jobs)).catch(() => undefined)
  }, [])
  useEffect(() => {
    loadWorkers()
    api.get<TokenInfo>('/api/workers/token').then(setTok).catch((e) => toast(e.message, 'bad'))
    api.get<{ sources: Source[]; attribution: string }>('/api/sources').then(setSources).catch(() => undefined)
    api.get<{ events: AuditEvent[] }>('/api/audit?limit=100').then((r) => setAudit(r.events)).catch(() => undefined)
    const h = window.setInterval(loadWorkers, 5000)
    return () => window.clearInterval(h)
  }, [loadWorkers, toast])
  useEffect(() => { setForm({ operator: settings.operator, aircraft_id: settings.aircraft_id, default_region: settings.default_region }) }, [settings])

  const save = async (s: Partial<Settings>) => {
    try {
      await saveSettings(s)
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  const doRotate = async () => {
    try {
      const r = await api.post<{ token: string }>('/api/workers/token/rotate', {})
      setTok((t) => (t ? { ...t, token: r.token } : t))
      setRotate(false)
      toast('Worker token changed: reconnect the GPU server')
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  const cancel = async (j: Job) => {
    try {
      await api.post(`/api/jobs/${j.id}/cancel`, {})
      loadWorkers()
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  const farmName = (id: string | null) => (id ? farms.find((f) => f.id === id)?.name ?? id : '–')
  const alive = workers?.remote.filter((w) => w.alive) ?? []
  const dirty = form.operator !== settings.operator || form.aircraft_id !== settings.aircraft_id || form.default_region !== settings.default_region

  return (
    <>
      <div className="page-head">
        <div>
          <h1>System</h1>
          <p className="muted">Compute, settings, data sources and the audit log for this console{meta ? ` (version ${meta.version}, ${meta.drone})` : ''}.</p>
        </div>
      </div>
      {meta?.lan_exposed ? <Notice kind="warn">This console is reachable from other computers on the network and has no login yet. Bind it to 127.0.0.1 unless the network is trusted.</Notice> : null}

      <div className="cols-main">
        <div>
          <section className="section" style={{ paddingTop: 0 }} aria-labelledby="gpu-h">
            <div className="section-head">
              <h2 id="gpu-h">GPU server</h2>
              {alive.length ? <Badge kind="ok">{alive.length} connected</Badge> : <Badge>not connected</Badge>}
            </div>
            <p className="small muted" style={{ marginBottom: 10 }}>Heavy jobs — iterations on large fields, weed detection on orthomosaics — run faster on a GPU server such as the H200.
              This computer runs {workers?.local ?? '…'} job{workers?.local === 1 ? '' : 's'} at a time on its own.</p>
            {workers?.remote.length ? (
              <div className="table-wrap" style={{ marginBottom: 12 }}><table className="table">
                <thead><tr><th>Worker</th><th>Status</th><th>Jobs it takes</th><th>Hardware</th></tr></thead>
                <tbody>{workers.remote.map((w) => (
                  <tr key={w.name}>
                    <td><b>{w.name}</b></td>
                    <td>{w.alive ? <Badge kind="ok">online</Badge> : <Badge kind="warn">last seen {fmt.minutes(w.seen_s_ago / 60)} ago</Badge>}</td>
                    <td className="small">{w.kinds.map((k) => JOB_NAMES[k] ?? k).join(', ')}</td>
                    <td className="small muted">{[w.info.gpu, w.info.python].filter(Boolean).join(' · ') || '–'}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            ) : null}
            <ol className="small" style={{ paddingLeft: 20, margin: '0 0 12px', display: 'flex', flexDirection: 'column', gap: 6 }}>
              <li>On this computer, run <code>scripts\console\Connect-GPU-Server.cmd</code> and enter the server address (for example <code>user@h200.example.org</code>).</li>
              <li>Type the server password when SSH asks. The console never sees or stores it.</li>
              <li>The script copies the code over the same SSH session, opens a tunnel to port {tok?.tunnel_port ?? 18099} and starts the worker. It appears above within a few seconds; iterations set to <i>Automatic</i> then run there.</li>
            </ol>
            <div className="btn-row">
              <button className="btn btn-s" onClick={() => setShow((s) => !s)}>{show ? 'Hide token' : 'Show token'}</button>
              <button className="btn btn-s btn-quiet" onClick={() => setRotate(true)}>Change token</button>
            </div>
            {show && tok ? (
              <div style={{ marginTop: 10 }}>
                <p className="xs muted">Worker token (the connect script reads it for you). To start a worker by hand inside an SSH session opened with <code>-R {tok.tunnel_port}:127.0.0.1:{tok.port}</code>:</p>
                <pre className="code" style={{ marginTop: 6, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{tok.token}</pre>
                <pre className="code xs" style={{ marginTop: 6, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>python -m fa_console.worker --server http://127.0.0.1:{tok.tunnel_port} --token {tok.token}</pre>
              </div>
            ) : null}
          </section>

          <section className="section" aria-labelledby="jobs-h">
            <div className="section-head"><h2 id="jobs-h">Jobs</h2><span className="xs muted">latest 30, refreshes every 5 s</span></div>
            {jobs.length ? (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Started</th><th>Job</th><th>Farm</th><th>Where</th><th>Status</th><th></th></tr></thead>
                <tbody>{jobs.map((j) => (
                  <tr key={j.id}>
                    <td className="num" style={{ whiteSpace: 'nowrap' }}>{fmt.time(j.created_at)}</td>
                    <td>{JOB_NAMES[j.kind] ?? j.kind}</td>
                    <td className="small">{farmName(j.farm_id)}</td>
                    <td className="small">{j.worker && !j.worker.startsWith('local') ? j.worker : j.target === 'remote' ? 'GPU server' : 'this computer'}</td>
                    <td><Badge kind={STATUS_KIND[j.status]}>{j.status === 'running' ? `running ${Math.round(j.progress * 100)}%` : j.status}</Badge>
                      {j.error ? <div className="xs" style={{ color: 'var(--alarm)', marginTop: 2 }}>{j.error.slice(0, 90)}</div> : null}</td>
                    <td>{j.status === 'running' || j.status === 'queued' ? <button className="btn btn-s btn-quiet" onClick={() => cancel(j)}>Cancel</button> : null}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            ) : <p className="small muted">No jobs yet.</p>}
          </section>

          <section className="section" aria-labelledby="audit-h">
            <div className="section-head"><h2 id="audit-h">Audit log</h2><span className="xs muted">who did what, latest 100</span></div>
            {audit.length ? (
              <div className="table-wrap" style={{ maxHeight: 420, overflowY: 'auto' }}><table className="table">
                <thead><tr><th>Time</th><th>Farm</th><th>Event</th><th>By</th><th>Details</th></tr></thead>
                <tbody>{[...audit].reverse().map((e) => (
                  <tr key={e.id}>
                    <td className="num" style={{ whiteSpace: 'nowrap' }}>{fmt.time(e.ts)}</td>
                    <td className="small">{farmName(e.farm_id)}</td>
                    <td>{e.kind.replace(/_/g, ' ')}</td>
                    <td className="small">{e.actor}</td>
                    <td className="xs muted" style={{ maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{JSON.stringify(e.payload).slice(0, 140)}</td>
                  </tr>
                ))}</tbody>
              </table></div>
            ) : <p className="small muted">Nothing recorded yet.</p>}
          </section>
        </div>

        <aside>
          <section className="section" style={{ paddingTop: 0 }} aria-labelledby="set-h">
            <h2 id="set-h" style={{ marginBottom: 12 }}>Settings</h2>
            <div className="field">
              <span>Theme</span>
              <Segmented label="Theme" value={settings.theme} onChange={(v) => save({ theme: v })}
                options={[{ value: 'dark', label: 'Dark' }, { value: 'light', label: 'Light' }, { value: 'auto', label: 'Match device' }]} />
            </div>
            <div className="field" style={{ marginTop: 12 }}>
              <span>Area unit</span>
              <Segmented label="Area unit" value={settings.area_unit} onChange={(v) => save({ area_unit: v })}
                options={[{ value: 'ha', label: 'hectare' }, { value: 'acre', label: 'acre' }, { value: 'mu', label: 'mu' }]} />
            </div>
            <div className="grid-form" style={{ gridTemplateColumns: '1fr', marginTop: 12 }}>
              <Field label="Operator name" hint="Written into spray records.">
                <input className="input" maxLength={80} value={form.operator} onChange={(e) => setForm({ ...form, operator: e.target.value })} />
              </Field>
              <Field label="Aircraft ID" hint="The K3-A Pro's registration or serial.">
                <input className="input" maxLength={80} value={form.aircraft_id} onChange={(e) => setForm({ ...form, aircraft_id: e.target.value })} />
              </Field>
              <Field label="Default region">
                <select className="input" value={form.default_region} onChange={(e) => setForm({ ...form, default_region: e.target.value })}>
                  <option value="">From the field's country</option>
                  {meta?.regions.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
                </select>
              </Field>
            </div>
            <button className="btn btn-primary" style={{ marginTop: 12 }} disabled={!dirty} onClick={() => save(form)}><Icon name="check" size={18} />Save settings</button>
          </section>

          <section className="section" aria-labelledby="src-h">
            <h2 id="src-h" style={{ marginBottom: 10 }}>Data sources</h2>
            {sources ? (
              <>
                <table className="table"><tbody>{sources.sources.map((s) => (
                  <tr key={s.layer}>
                    <td className="small"><b>{s.layer}</b><div><a href={s.url} target="_blank" rel="noreferrer">{s.source}</a></div></td>
                    <td className="xs muted">{s.licence}</td>
                  </tr>
                ))}</tbody></table>
                <p className="xs faint" style={{ marginTop: 8 }}>{sources.attribution}</p>
              </>
            ) : <p className="small muted">Loading…</p>}
          </section>
        </aside>
      </div>

      {rotate ? (
        <Dialog title="Change the worker token?" onClose={() => setRotate(false)}
          actions={<><button className="btn" onClick={() => setRotate(false)}>Keep it</button><button className="btn btn-primary" onClick={doRotate}>Change token</button></>}>
          <p>Connected GPU workers stop receiving jobs until they reconnect with the new token. Do this if the token may have leaked.</p>
        </Dialog>
      ) : null}
    </>
  )
}
