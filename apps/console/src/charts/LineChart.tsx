import { useEffect, useRef, useState } from 'react'
import {
  BarController, BarElement, CategoryScale, Chart, Filler, LinearScale, LineController, LineElement, PointElement, Tooltip,
  type ChartConfiguration, type Plugin,
} from 'chart.js'

Chart.register(LineController, LineElement, PointElement, BarController, BarElement, CategoryScale, LinearScale, Tooltip, Filler)

// The validated categorical order (light / dark); fixed, never cycled.
const SERIES_LIGHT = ['#2a78d6', '#1baf7a', '#eda100', '#6250d6', '#e87ba4']
const SERIES_DARK = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']

export interface Series { label: string; data: (number | null)[]; slot?: number; dashed?: boolean; kind?: 'line' | 'bar' }
export interface Marker { at: string; kind: 'flight' | 'scan' }

function css(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

function dark(): boolean {
  const a = document.documentElement.getAttribute('data-theme')
  return a ? a === 'dark' : (window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false)
}

export function seriesColor(slot: number): string {
  return (dark() ? SERIES_DARK : SERIES_LIGHT)[slot % 5]
}

/** Vertical marks for flights (survey orange) and scans (forecast blue), plus an optional limit line. */
const marksPlugin: Plugin<'line' | 'bar'> = {
  id: 'faMarks',
  afterDatasetsDraw(chart, _args, opts) {
    const o = opts as { markers?: Marker[]; limit?: number | null; labels?: string[] }
    const { ctx, chartArea: a, scales } = chart
    const x = scales.x, y = scales.y
    ctx.save()
    for (const m of o.markers ?? []) {
      const i = o.labels?.indexOf(m.at) ?? -1
      if (i < 0) continue
      const px = x.getPixelForValue(i)
      ctx.strokeStyle = m.kind === 'flight' ? css('--survey') : css('--sky')
      ctx.lineWidth = 1
      ctx.globalAlpha = m.kind === 'flight' ? 0.7 : 0.4
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke()
    }
    if (o.limit !== null && o.limit !== undefined) {
      const py = y.getPixelForValue(o.limit)
      ctx.globalAlpha = 0.9
      ctx.setLineDash([5, 4])
      ctx.strokeStyle = css('--alarm')
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke()
    }
    // direct labels at the end of each line (<= 4 series)
    const sets = chart.data.datasets
    if (sets.length >= 2 && sets.length <= 4) {
      ctx.setLineDash([])
      ctx.globalAlpha = 1
      ctx.font = `600 11px ${css('--font-cond') || 'sans-serif'}`
      ctx.fillStyle = css('--ink-2')
      const tags = sets.map((ds, k) => {
        const meta = chart.getDatasetMeta(k)
        const pts = meta.data.filter((_p, i) => (ds.data as (number | null)[])[i] !== null)
        const last = pts[pts.length - 1]
        return last ? { text: String(ds.label), x: last.x + 6, y: last.y } : null
      }).filter((v): v is { text: string; x: number; y: number } => v !== null).sort((p, q) => p.y - q.y)
      for (let k = 1; k < tags.length; k++) {           // keep labels at least 13 px apart
        if (tags[k].y - tags[k - 1].y < 13) tags[k].y = tags[k - 1].y + 13
      }
      for (const tg of tags) ctx.fillText(tg.text, Math.min(tg.x, a.right + 4), tg.y + 4)
    }
    ctx.restore()
  },
}

export function LineChart({ labels, series, yTitle, markers, limit, height = 220, yMin, ariaLabel }: {
  labels: string[]; series: Series[]; yTitle: string; markers?: Marker[]; limit?: number | null; height?: number; yMin?: number; ariaLabel: string
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  const [table, setTable] = useState(false)
  useEffect(() => {
    if (!ref.current || table) return
    const ink = css('--ink-2'), rule = css('--rule'), font = css('--font-cond') || 'sans-serif'
    const cfg: ChartConfiguration<'line' | 'bar'> = {
      type: 'line',
      data: {
        labels,
        datasets: series.map((s) => {
          const c = seriesColor(s.slot ?? 0)
          return s.kind === 'bar'
            ? { type: 'bar' as const, label: s.label, data: s.data, backgroundColor: c, borderRadius: 2, barPercentage: 0.9, categoryPercentage: 1 }
            : { type: 'line' as const, label: s.label, data: s.data, borderColor: c, backgroundColor: c, borderWidth: 2, pointRadius: 0,
                pointHoverRadius: 4, borderDash: s.dashed ? [6, 4] : undefined, spanGaps: true, tension: 0.25 }
        }),
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false,
        interaction: { mode: 'index', intersect: false },
        layout: { padding: { right: series.length >= 2 ? 64 : 8, top: 6 } },
        scales: {
          x: { grid: { display: false }, ticks: { color: ink, maxTicksLimit: 8, maxRotation: 0, font: { family: font, size: 11 } }, border: { color: rule } },
          y: { min: yMin, grid: { color: rule }, border: { display: false }, ticks: { color: ink, font: { family: font, size: 11 }, maxTicksLimit: 6 },
               title: { display: true, text: yTitle, color: ink, font: { family: font, size: 11 } } },
        },
        plugins: {
          tooltip: { backgroundColor: css('--ink'), titleColor: css('--paper'), bodyColor: css('--paper'), padding: 8, displayColors: true },
          // @ts-expect-error custom plugin options
          faMarks: { markers, limit, labels },
        },
      },
      plugins: [marksPlugin],
    }
    const chart = new Chart(ref.current, cfg)
    return () => chart.destroy()
  }, [labels, series, yTitle, markers, limit, yMin, table])

  return (
    <figure className="chart" style={{ margin: 0 }}>
      {series.length >= 2 ? (
        <div className="chart-legend" aria-hidden="true">
          {series.map((s) => (
            <span key={s.label}><i style={{ background: seriesColor(s.slot ?? 0), height: s.kind === 'bar' ? 10 : 3 }} />{s.label}</span>
          ))}
        </div>
      ) : null}
      {table ? (
        <div className="table-wrap" style={{ maxHeight: height, overflowY: 'auto' }}>
          <table className="table">
            <thead><tr><th>Day</th>{series.map((s) => <th key={s.label} className="n">{s.label}</th>)}</tr></thead>
            <tbody>{labels.map((l, i) => <tr key={l}><td>{l}</td>{series.map((s) => <td key={s.label} className="n">{s.data[i] ?? '–'}</td>)}</tr>)}</tbody>
          </table>
        </div>
      ) : (
        <div style={{ height, position: 'relative' }}><canvas ref={ref} role="img" aria-label={ariaLabel} /></div>
      )}
      <button className="btn btn-quiet btn-s" style={{ marginTop: 4 }} onClick={() => setTable(!table)}>{table ? 'Show chart' : 'Show table'}</button>
    </figure>
  )
}
