import { useEffect, useState } from 'react'
import { api, type Farm, type GeoJSON, type Job } from '../api'
import { useApp } from '../state'
import { go, href } from '../router'
import { Icon } from '../ui/icons'
import { area, Badge, Dialog, Empty, fmt, JobBar, STATUS_KIND, useJob } from '../ui/core'

export function Farms() {
  const { farms, t, meta } = useApp()
  const templates = Object.fromEntries((meta?.templates ?? []).map((x) => [x.id, x]))
  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t('nav.farms')}</h1>
          <p className="muted">Each farm is a digital twin built from real data. Open one to run its mission.</p>
        </div>
        <a className="btn btn-primary" href={href.newFarm}><Icon name="plus" />{t('act.add_farm')}</a>
      </div>
      {farms.length === 0 ? (
        <div className="sheet">
          <Empty title="No farms yet"
            action={<a className="btn btn-primary btn-l" href={href.newFarm}><Icon name="plus" />{t('act.add_farm')}</a>}>
            Start with a template site to rehearse a whole season on real data, or add your own field by
            clicking it on the map, drawing it, or uploading its boundary.
          </Empty>
        </div>
      ) : (
        <div className="rows">
          {farms.map((f) => <FarmRow key={f.id} farm={f} templateName={templates[f.template]?.name} />)}
        </div>
      )}
    </>
  )
}

function FarmRow({ farm, templateName }: { farm: Farm; templateName?: string }) {
  const { t, settings, reloadFarms, toast } = useApp()
  const [confirm, setConfirm] = useState(false)
  const open = () => farm.status === 'ready' && go(href.farm(farm.id))
  const remove = async () => {
    try {
      await api.del(`/api/farms/${farm.id}`)
      toast(`Deleted ${farm.name}`)
      setConfirm(false)
      reloadFarms()
    } catch (e) {
      toast((e as Error).message, 'bad')
    }
  }
  return (
    <div className={`row${farm.status === 'ready' ? ' link' : ''}`} style={{ gridTemplateColumns: '64px minmax(0, 2fr) minmax(0, 1.4fr) auto' }}
      onClick={open} onKeyDown={(e) => { if (e.key === 'Enter') open() }} tabIndex={farm.status === 'ready' ? 0 : -1}>
      <Outline polygon={farm.polygon} />
      <div style={{ minWidth: 0 }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          <h3 style={{ fontSize: 'var(--fs-l)' }}>{farm.name}</h3>
          <Badge kind={farm.mode === 'field' ? 'live' : 'info'}>{t(`mode.${farm.mode}`)}</Badge>
        </div>
        <div className="small muted">{templateName ?? farm.template} — {farm.crop}, {area(farm.area_ha, settings.area_unit)}, region {farm.region}</div>
      </div>
      <div className="small" style={{ minWidth: 0 }}>
        {farm.status === 'building' || farm.status === 'queued' ? <BuildProgress fid={farm.id} />
          : farm.status === 'error' ? <span style={{ color: 'var(--alarm)' }}>{farm.meta.error}</span>
          : <span className="muted">Season {fmt.date(farm.sowing)} to {fmt.date(farm.harvest)}</span>}
      </div>
      <div className="btn-row" onClick={(e) => e.stopPropagation()}>
        <Badge kind={STATUS_KIND[farm.status]}>{farm.status}</Badge>
        {farm.status === 'error' ? (
          <button className="btn btn-s" onClick={async () => { await api.post(`/api/farms/${farm.id}/build`); reloadFarms() }}>Build again</button>
        ) : null}
        <button className="btn btn-quiet btn-s" aria-label={`Delete ${farm.name}`} onClick={() => setConfirm(true)}><Icon name="trash" size={18} /></button>
      </div>
      {confirm ? (
        <Dialog title={`Delete ${farm.name}?`} onClose={() => setConfirm(false)}
          actions={<><button className="btn" onClick={() => setConfirm(false)}>{t('act.cancel')}</button><button className="btn btn-danger" onClick={remove}>Delete farm</button></>}>
          <p>This removes the digital farm, its mission, plans, records and device readings from this computer.
            Export a bundle from Records first if you need them.</p>
        </Dialog>
      ) : null}
    </div>
  )
}

function BuildProgress({ fid }: { fid: string }) {
  const [jobId, setJobId] = useState<string | null>(null)
  useEffect(() => {
    api.get<{ jobs: Job[] }>(`/api/jobs?farm_id=${fid}&kind=build&limit=1`).then((r) => setJobId(r.jobs[0]?.id ?? null)).catch(() => undefined)
  }, [fid])
  const job = useJob(jobId)
  return job ? <JobBar job={job} /> : <span className="muted">Queued</span>
}

export function Outline({ polygon, size = 56 }: { polygon: GeoJSON | null; size?: number }) {
  const ring = ringOf(polygon)
  if (!ring) return <div style={{ width: size, height: size, borderRadius: 8, background: 'var(--paper-2)' }} />
  const lat0 = ring.reduce((a, p) => a + p[1], 0) / ring.length
  const k = Math.cos((lat0 * Math.PI) / 180)
  const xs = ring.map((p) => p[0] * k)
  const ys = ring.map((p) => p[1])
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys)
  const s = (size - 10) / Math.max(maxx - minx, maxy - miny, 1e-9)
  const pts = ring.map((_, i) => `${5 + (xs[i] - minx) * s},${size - 5 - (ys[i] - miny) * s}`).join(' ')
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true" style={{ background: 'var(--paper-2)', borderRadius: 8 }}>
      <polygon points={pts} fill="var(--canopy-soft)" stroke="var(--canopy)" strokeWidth={1.6} />
    </svg>
  )
}

export function ringOf(g: GeoJSON | null | undefined): [number, number][] | null {
  if (!g) return null
  const geom = (g.type === 'Feature' ? (g.geometry as GeoJSON) : g) as GeoJSON
  if (geom.type === 'Polygon') return (geom.coordinates as [number, number][][])[0]
  if (geom.type === 'MultiPolygon') return (geom.coordinates as [number, number][][][])[0][0]
  return null
}
