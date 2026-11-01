import { cloneElement, isValidElement, useEffect, useId, useRef, useState, type ReactElement, type ReactNode } from 'react'
import { watchJob, type Job } from '../api'
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
    </div>
  )
}

// ------------------------------------------------------------------ formatting
export const fmt = {
  n: (v: number | null | undefined, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? '–' : v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 })),
  money: (v: number | null | undefined, cur: string) => (v === null || v === undefined ? '–' : `${Math.round(v).toLocaleString()} ${cur}`),
  pct: (v: number | null | undefined, d = 0) => (v === null || v === undefined ? '–' : `${(v * 100).toFixed(d)}%`),
  date: (s: string | null | undefined) => {
    if (!s) return '–'
    const d = new Date(s.length === 10 ? s + 'T12:00:00' : s)
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
  },
  short: (s: string | null | undefined) => {
    if (!s) return '–'
    const d = new Date(s.length === 10 ? s + 'T12:00:00' : s)
    return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  },
  time: (s: string | null | undefined) => (s ? new Date(s).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '–'),
  minutes: (m: number | null | undefined) => {
    if (m === null || m === undefined) return '–'
    if (m < 60) return `${Math.round(m)} min`
    const h = Math.floor(m / 60)
    return `${h} h ${Math.round(m - 60 * h)} min`
  },
}

export function area(ha: number | null | undefined, unit: 'ha' | 'acre' | 'mu' = 'ha'): string {
  if (ha === null || ha === undefined) return '–'
  if (unit === 'acre') return `${(ha * 2.4710538).toFixed(ha < 4 ? 2 : 1)} ac`
  if (unit === 'mu') return `${(ha * 15).toFixed(ha < 1 ? 2 : 1)} mu`
  return `${ha.toFixed(ha < 10 ? 2 : 1)} ha`
}
