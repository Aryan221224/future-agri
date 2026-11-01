import { LOCALE } from '../i18n'
import { cloneElement, isValidElement, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from 'react'
import { watchJob, type Job, type JobDetail } from '../api'
import { Icon } from './icons'

export function Badge({ kind = '', children }: { kind?: '' | 'ok' | 'live' | 'info' | 'warn' | 'bad' | 'plain'; children: ReactNode }) {
  return <span className={`badge ${kind}`}>{children}</span>
}

export const STATUS_KIND: Record<string, '' | 'ok' | 'live' | 'info' | 'warn' | 'bad'> = {
  ready: 'ok', done: 'ok', flown: 'ok', approved: 'info', proposed: 'warn', queued: '', building: 'live', running: 'live',
  error: 'bad', failed: 'bad', rejected: '', dropped: 'bad', cancelled: '', draft: 'warn', superseded: '', new: '',
}

export function Stat({ value, label, note }: { value: ReactNode; label: ReactNode; note?: ReactNode }) {
  return (
    <div className="stat">
      <b>{value}</b>
      <span>{label}</span>
      {note ? <small>{note}</small> : null}
    </div>
  )
}

export function Progress({ value, indeterminate }: { value?: number; indeterminate?: boolean }) {
  return (
    <div className={`progress${indeterminate ? ' indet' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round((value ?? 0) * 100)}>
      <i style={{ width: `${Math.round((value ?? 0) * 100)}%` }} />
    </div>
  )
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children ? <p>{children}</p> : null}
      {action}
    </div>
  )
}

export function Notice({ kind = '', children }: { kind?: '' | 'warn' | 'bad' | 'ok'; children: ReactNode }) {
  return <div className={`notice ${kind}`}>{children}</div>
}

export function Source({ children }: { children: ReactNode }) {
  return <span className="chip"><Icon name="satellite" />{children}</span>
}

/** A labelled control: the label names it, the hint describes it (not part of its name). */
export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  const id = useId()
  const child = isValidElement(children)
    ? cloneElement(children as ReactElement<{ id?: string; 'aria-describedby'?: string }>, { id, 'aria-describedby': hint ? `${id}-h` : undefined })
    : children
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {child}
      {hint ? <small id={`${id}-h`}>{hint}</small> : null}
    </div>
  )
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string
}) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  )
}

export function Dialog({ title, children, onClose, actions }: { title: string; children: ReactNode; onClose: () => void; actions?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null
    ref.current?.querySelector<HTMLElement>('button, input, select, textarea')?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => { window.removeEventListener('keydown', onKey); prev?.focus() }
  }, [onClose])
  return (
    <div className="dialog-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="dialog" role="dialog" aria-modal="true" aria-label={title} ref={ref}>
        <h3>{title}</h3>
        {children}
        {actions ? <div className="btn-row" style={{ justifyContent: 'flex-end' }}>{actions}</div> : null}
      </div>
    </div>
  )
}

/** Follow a job to its end; returns its live state. */
export function useJob(id: string | null | undefined, onEnd?: (j: Partial<Job>) => void) {
  const [job, setJob] = useState<Partial<Job> | null>(null)
  const endRef = useRef(onEnd)
  endRef.current = onEnd
  useEffect(() => {
    if (!id) { setJob(null); return }
    setJob({ id, status: 'queued', progress: 0, message: '' })
    return watchJob(id, (j) => {
      setJob((old) => ({ ...(old || {}), ...j }))
      if (j.status === 'done' || j.status === 'failed' || j.status === 'cancelled') endRef.current?.(j)
    })
  }, [id])
  return job
}

export function JobBar({ job, label }: { job: Partial<Job> | null; label?: string }) {
  if (!job) return null
  const failed = job.status === 'failed'
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }} className="small">
        <span>{label ? <b>{label}: </b> : null}{failed ? job.error : job.message || (job.status === 'queued' ? 'Waiting to start' : 'Working')}</span>
        <span className="num muted">{job.status === 'done' ? 'done' : failed ? 'failed' : `${Math.round((job.progress ?? 0) * 100)}%`}</span>
      </div>
      {failed ? null : <Progress value={job.progress ?? 0} indeterminate={job.status === 'queued'} />}
      {job.status === 'running' && job.detail ? <SubLoader d={job.detail} /> : null}
    </div>
  )
}

const bytes = (b: number) => (b >= 1e6 ? `${(b / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1e3))} kB`)
const secs = (s: number) => (s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`)

/** The smaller loader for the step in progress: restarts with each step and shows what can be
 * measured -- items done, data downloaded, download rate, time left -- and the step's elapsed time. */
export function SubLoader({ d }: { d: JobDetail }) {
  const [, tick] = useState(0)
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), 1000)
    return () => window.clearInterval(t)
  }, [])
  const started = d.started ? Date.parse(d.started.endsWith('Z') || d.started.includes('+') ? d.started : d.started + 'Z') : NaN
  const elapsed = Number.isFinite(started) ? Math.max(0, (Date.now() - started) / 1000) : null
  const counted = typeof d.done === 'number' && typeof d.total === 'number' && d.total > 0
  const parts: string[] = []
  if (counted) parts.push(`${d.done}/${d.total} ${d.unit ?? ''}`.trim())
  if (typeof d.bytes === 'number' && d.bytes > 0) parts.push(`${d.approx ? '≈' : ''}${bytes(d.bytes)}`)
  if (typeof d.rate_bps === 'number' && d.rate_bps > 0) parts.push(`${bytes(d.rate_bps)}/s`)
  if (typeof d.eta_s === 'number' && d.eta_s > 0) parts.push(`about ${secs(d.eta_s)} left`)
  if (elapsed !== null) parts.push(`${secs(elapsed)} on this step`)
  return (
    <div className="subloader" data-testid="subloader">
      <div className="xsmall muted" style={{ display: 'flex', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <span>{d.label || d.step || 'Working'}</span>
        <span className="num">{parts.join(' · ')}</span>
      </div>
      <Progress value={counted ? (d.done as number) / (d.total as number) : 0} indeterminate={!counted} />
    </div>
  )
}

// ------------------------------------------------------------------ formatting
export const fmt = {
  n: (v: number | null | undefined, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? '–' : v.toLocaleString(LOCALE, { maximumFractionDigits: d, minimumFractionDigits: 0 })),
  money: (v: number | null | undefined, cur: string) => (v === null || v === undefined ? '–' : `${Math.round(v).toLocaleString(LOCALE)} ${cur}`),
  pct: (v: number | null | undefined, d = 0) => (v === null || v === undefined ? '–' : `${(v * 100).toFixed(d)}%`),
  date: (s: string | null | undefined) => {
    if (!s) return '–'
    const d = new Date(s.length === 10 ? s + 'T12:00:00' : s)
    return d.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short', year: 'numeric' })
  },
  short: (s: string | null | undefined) => {
    if (!s) return '–'
    const d = new Date(s.length === 10 ? s + 'T12:00:00' : s)
    return d.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short' })
  },
  time: (s: string | null | undefined) => (s ? new Date(s).toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' }) : '–'),
  minutes: (m: number | null | undefined) => {
    if (m === null || m === undefined) return '–'
    if (m < 60) return `${Math.round(m)} min`
    const h = Math.floor(m / 60)
    return `${h} h ${Math.round(m - 60 * h)} min`
  },
  /** "1 season", "3 seasons", "1.5 scans" */
  count: (v: number | null | undefined, one: string, many = one + 's', d = 1) => (v === null || v === undefined ? '–' : `${fmt.n(v, d)} ${v === 1 ? one : many}`),
}

export function area(ha: number | null | undefined, unit: 'ha' | 'acre' | 'mu' = 'ha'): string {
  if (ha === null || ha === undefined) return '–'
  if (unit === 'acre') return `${(ha * 2.4710538).toFixed(ha < 4 ? 2 : 1)} ac`
  if (unit === 'mu') return `${(ha * 15).toFixed(ha < 1 ? 2 : 1)} mu`
  return `${ha.toFixed(ha < 10 ? 2 : 1)} ha`
}
