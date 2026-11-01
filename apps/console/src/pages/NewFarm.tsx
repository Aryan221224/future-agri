import 'leaflet/dist/leaflet.css'
import L from 'leaflet'
import { useEffect, useMemo, useRef, useState } from 'react'
import { api, type Farm, type GeoJSON, type Job, type Mode, type Template } from '../api'
import { useApp } from '../state'
import { go, href } from '../router'
import { Icon } from '../ui/icons'
import { Field, JobBar, Notice, Segmented, useJob } from '../ui/core'
import { COUNTRIES } from './countries'

type Where = 'site' | 'point' | 'draw'
const EEA = new Set(['AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE', 'IS', 'LI', 'NO'])

export default function NewFarm() {
  const { meta, reloadFarms, t } = useApp()
  const templates = useMemo(() => (meta?.templates ?? []).filter((x) => x.supported), [meta])
  const [step, setStep] = useState<1 | 2 | 3>(1)
  const [where, setWhere] = useState<Where>('site')
  const [tid, setTid] = useState('A5')
  const [site, setSite] = useState(0)
  const [point, setPoint] = useState<{ lat: number; lon: number } | null>(null)
  const [country, setCountry] = useState('NL')
  const [polygon, setPolygon] = useState<GeoJSON | null>(null)
  const [mode, setMode] = useState<Mode>('sim')
  const [year, setYear] = useState<string>('')
  const [sowing, setSowing] = useState<string>('')
  const [region, setRegion] = useState<string>('')
  const [cell, setCell] = useState<number>(10)
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [farmId, setFarmId] = useState<string | null>(null)
  const tpl: Template | undefined = templates.find((x) => x.id === tid)

  useEffect(() => { if (tpl) setCountry(tpl.locations[site]?.country ?? tpl.locations[0].country) }, [tpl, site])

  const job = useJob(jobId, async (j) => {
    await reloadFarms()
    if (j.status === 'done' && farmId) go(href.farm(farmId))
  })

  const whereOk = where === 'site' || (where === 'point' ? !!polygon || (!!point && !!country) : !!polygon)
  const suggestedRegion = region || (where === 'site' ? tpl?.region : EEA.has(country) ? 'EU' : (meta?.regions.some((r) => r.id === country) ? country : 'GLOBAL')) || 'GLOBAL'

  const submit = async () => {
    setError(null)
    const body: Record<string, unknown> = {
      name: name.trim() || `${tpl?.name ?? 'New'} field`, mode, template: tid, cell_m: cell, region: suggestedRegion,
    }
    if (where === 'site' && !polygon) body.site_index = site
    if (polygon) body.polygon = polygon
    else if (where === 'point' && point) { body.lat = point.lat; body.lon = point.lon; body.country = country }
    if (country) body.country = country
    if (mode === 'sim' && year) body.year = Number(year)
    if (mode === 'field') body.sowing = sowing
    try {
      const r = await api.post<{ farm: Farm; job: Job }>('/api/farms', body)
      setFarmId(r.farm.id)
      setJobId(r.job.id)
      setStep(3)
      reloadFarms()
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const years = useMemo(() => { const y = new Date().getFullYear(); return Array.from({ length: 10 }, (_, i) => y - i) }, [])

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Add a farm</h1>
          <p className="muted">The console builds the farm's digital twin from real data: field boundary, soil, terrain, weather and satellite.</p>
        </div>
        <a className="btn btn-quiet" href={href.farms}><Icon name="back" />Farms</a>
      </div>
      <ol className="loop" style={{ listStyle: 'none', padding: 0, margin: '0 0 20px', maxWidth: 760 }} aria-label="Steps">
        {['Where is the field?', 'What is growing?', 'Build the digital farm'].map((s, i) => (
          <li key={s} className={`loop-step${step === i + 1 ? ' now' : step > i + 1 ? ' done' : ''}`}>
            <i>Step {i + 1}</i><b>{s}</b>
          </li>
        ))}
      </ol>

      {step === 1 ? (
        <div className="cols-main">
          <div className="sheet sheet-pad" style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
            <Segmented label="How to locate the field" value={where} onChange={(v) => { setWhere(v); setPolygon(null) }}
              options={[{ value: 'site', label: 'Template site' }, { value: 'point', label: 'Find on the map' }, { value: 'draw', label: 'Draw or upload' }]} />
            {where === 'site' ? (
              <SitePicker templates={templates} tid={tid} setTid={setTid} site={site} setSite={setSite} />
            ) : (
              <>
                <MapPicker where={where} point={point} setPoint={setPoint} polygon={polygon} setPolygon={setPolygon}
                  country={country} center={tpl ? [tpl.locations[site]?.lat ?? 0, tpl.locations[site]?.lon ?? 0] : [20, 0]} />
                {where === 'point' ? (
                  <Field label="Country" hint="Field boundaries are looked up in the Fields of The World data for this country.">
                    <select className="input" value={country} onChange={(e) => setCountry(e.target.value)}>
                      {COUNTRIES.map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                    </select>
                  </Field>
                ) : null}
              </>
            )}
          </div>
          <aside style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <WhereHelp where={where} />
            <button className="btn btn-primary btn-l" disabled={!whereOk} onClick={() => setStep(2)}>Continue</button>
          </aside>
        </div>
      ) : null}

      {step === 2 ? (
        <div className="cols-main">
          <div className="sheet sheet-pad" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
            <Field label="Crop template" hint={tpl ? `${tpl.crop} (${tpl.variety}); season ${tpl.sowing} to ${tpl.harvest}; ${tpl.drone_use}` : undefined}>
              <select className="input" value={tid} onChange={(e) => setTid(e.target.value)}>
                {templates.map((x) => <option key={x.id} value={x.id}>{x.name} — {x.crop}</option>)}
              </select>
            </Field>
            {tpl?.fidelity_note ? <Notice kind="warn">{tpl.fidelity_note}</Notice> : null}
            <div className="field">
              <span>Kind of farm</span>
              <Segmented label="Kind of farm" value={mode} onChange={setMode}
                options={[{ value: 'sim', label: t('mode.sim') }, { value: 'field', label: t('mode.field') }]} />
              <small>{mode === 'sim'
                ? 'A past season with complete data. The real field is rebuilt with a hidden ground truth, so you can rehearse the whole mission and score the result.'
                : 'This season, as it happens. Weather and satellite arrive live; you upload scans and import the drone\'s job records.'}</small>
            </div>
            <div className="grid-form">
              {mode === 'sim' ? (
                <Field label="Season">
                  <select className="input" value={year} onChange={(e) => setYear(e.target.value)}>
                    <option value="">Latest complete season</option>
                    {years.map((y) => <option key={y} value={y}>{y}</option>)}
                  </select>
                </Field>
              ) : (
                <Field label="Sowing or transplanting date">
                  <input className="input" type="date" value={sowing} onChange={(e) => setSowing(e.target.value)} required />
                </Field>
              )}
              <Field label="Regulation and price pack" hint="Sets wind limits, buffer zones, record keeping and currency.">
                <select className="input" value={suggestedRegion} onChange={(e) => setRegion(e.target.value)}>
                  {(meta?.regions ?? []).map((r) => <option key={r.id} value={r.id}>{r.name}{r.verified ? '' : ' (unverified)'}</option>)}
                </select>
              </Field>
              <Field label="Cell size" hint="Smaller cells show more detail; large fields are capped at 20,000 cells.">
                <select className="input" value={cell} onChange={(e) => setCell(Number(e.target.value))}>
                  {[5, 10, 20].map((c) => <option key={c} value={c}>{c} m</option>)}
                </select>
              </Field>
              <Field label="Farm name">
                <input className="input" value={name} placeholder={`${tpl?.name ?? 'New'} field`} onChange={(e) => setName(e.target.value)} maxLength={80} />
              </Field>
            </div>
            {error ? <Notice kind="bad">{error}</Notice> : null}
          </div>
          <aside style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
            <button className="btn btn-primary btn-l" disabled={mode === 'field' && !sowing} onClick={submit}>Build the digital farm</button>
            <button className="btn" onClick={() => setStep(1)}>Back</button>
          </aside>
        </div>
      ) : null}

      {step === 3 ? (
        <div className="sheet sheet-pad" style={{ maxWidth: 720, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <h2>Building the digital farm</h2>
          <p className="muted">Real data is fetched and cached: boundary, soil (SoilGrids), terrain (Copernicus DEM), weather
            (NASA POWER) and satellite (Sentinel-2). For a new location this takes a few minutes; template sites are faster.</p>
          <JobBar job={job} />
          {job?.status === 'failed' ? (
            <div className="btn-row">
              <button className="btn" onClick={() => { setStep(where === 'site' ? 2 : 1); setJobId(null) }}>Change the field</button>
              <a className="btn btn-quiet" href={href.farms}>Back to farms</a>
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  )
}

function SitePicker({ templates, tid, setTid, site, setSite }: {
  templates: Template[]; tid: string; setTid: (s: string) => void; site: number; setSite: (n: number) => void
}) {
  return (
    <div className="rows" role="radiogroup" aria-label="Template site">
      {templates.flatMap((x) => x.locations.map((loc, i) => {
        const on = tid === x.id && site === i
        return (
          <label key={`${x.id}-${i}`} className="row link" style={{ gridTemplateColumns: '24px 1fr auto', background: on ? 'var(--canopy-soft)' : undefined }}>
            <input type="radio" name="site" checked={on} onChange={() => { setTid(x.id); setSite(i) }} style={{ accentColor: 'var(--canopy)', width: 18, height: 18 }} />
            <span><b>{loc.name}</b><br /><span className="small muted">{x.name}: {x.crop}, fields {x.field_ha[0]}–{x.field_ha[1]} ha</span></span>
            <span className="small faint num">{loc.lat.toFixed(2)}, {loc.lon.toFixed(2)}</span>
          </label>
        )
      }))}
    </div>
  )
}

function WhereHelp({ where }: { where: Where }) {
  const text = {
    site: 'A real field near a research site for the crop is picked automatically from the Fields of The World boundaries.',
    point: 'Click the field on the map. The console finds the field boundaries around that point; pick yours from the list.',
    draw: 'Click the corners of the field on the map and finish the boundary, or upload a GeoJSON or KML file of it.',
  }[where]
  return <Notice>{text}</Notice>
}

function MapPicker({ where, point, setPoint, polygon, setPolygon, country, center }: {
  where: Where; point: { lat: number; lon: number } | null; setPoint: (p: { lat: number; lon: number }) => void
  polygon: GeoJSON | null; setPolygon: (g: GeoJSON | null) => void; country: string; center: [number, number]
}) {
  const el = useRef<HTMLDivElement>(null)
  const map = useRef<L.Map | null>(null)
  const layer = useRef<L.LayerGroup | null>(null)
  const [verts, setVerts] = useState<[number, number][]>([])
  const [cands, setCands] = useState<GeoJSON[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [source, setSource] = useState<string | null>(null)
  const whereRef = useRef(where)
  whereRef.current = where

  useEffect(() => {
    if (!el.current || map.current) return
    const m = L.map(el.current, { center, zoom: 15, zoomControl: true })
    const osm = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '© OpenStreetMap contributors' })
    const sat = L.tileLayer('https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless_3857/default/g/{z}/{y}/{x}.jpg',
      { maxZoom: 17, attribution: 'Sentinel-2 cloudless 2016 by EOX (CC BY 4.0)' })
    sat.addTo(m)
    L.control.layers({ 'Satellite (2016)': sat, Map: osm }).addTo(m)
    layer.current = L.layerGroup().addTo(m)
    m.on('click', (e: L.LeafletMouseEvent) => {
      if (whereRef.current === 'point') { setPoint({ lat: e.latlng.lat, lon: e.latlng.lng }); setCands(null); setPolygon(null) }
      else setVerts((v) => [...v, [e.latlng.lng, e.latlng.lat]])
    })
    map.current = m
    return () => {
      map.current = null
      try { m.off(); m.stop(); m.remove() } catch { /* map already torn down */ }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => { setVerts([]); setCands(null); setMsg(null) }, [where])

  useEffect(() => {
    const g = layer.current
    if (!g) return
    g.clearLayers()
    const style = { color: '#f25c05', weight: 2, fillOpacity: 0.15 }
    if (point && where === 'point') L.circleMarker([point.lat, point.lon], { radius: 6, color: '#f25c05', fillOpacity: 1 }).addTo(g)
    if (cands) for (const c of cands) {
      const ring = (((c.geometry as GeoJSON).coordinates as [number, number][][])[0]).map(([x, y]) => [y, x] as [number, number])
      const sel = polygon && JSON.stringify((polygon as GeoJSON).coordinates) === JSON.stringify((c.geometry as GeoJSON).coordinates)
      L.polygon(ring, { color: sel ? '#1e7a46' : '#2b6cb0', weight: sel ? 3 : 1.5, fillOpacity: sel ? 0.3 : 0.08 })
        .on('click', (ev) => { L.DomEvent.stopPropagation(ev); setPolygon(c.geometry as GeoJSON) }).addTo(g)
    }
    if (polygon && !cands) {
      const ring = ((polygon.coordinates as [number, number][][])[0]).map(([x, y]) => [y, x] as [number, number])
      const pg = L.polygon(ring, { ...style, color: '#1e7a46' }).addTo(g)
      map.current?.fitBounds(pg.getBounds(), { maxZoom: 17, padding: [20, 20], animate: false })
    }
    if (verts.length) {
      L.polyline(verts.map(([x, y]) => [y, x] as [number, number]), style).addTo(g)
      verts.forEach(([x, y]) => L.circleMarker([y, x], { radius: 4, color: '#f25c05', fillOpacity: 1 }).addTo(g))
    }
  }, [point, cands, polygon, verts, where])

  const detect = async () => {
    if (!point) return
    setBusy(true); setMsg(null)
    try {
      const r = await api.post<{ features: GeoJSON[] }>('/api/boundary/detect', { lat: point.lat, lon: point.lon, country })
      setCands(r.features)
      const inside = r.features.find((f) => f.properties?.contains_point)
      if (inside) setPolygon(inside.geometry as GeoJSON)
      setMsg(r.features.length ? `${r.features.length} fields found nearby${inside ? '; the one you clicked is selected' : ''}. Click a field to choose it.`
        : 'No field boundary found here. Draw or upload the boundary instead.')
    } catch (e) {
      setMsg(`${(e as Error).message}. You can draw or upload the boundary instead.`)
    } finally {
      setBusy(false)
    }
  }

  const finish = () => {
    if (verts.length < 3) return
    setPolygon({ type: 'Polygon', coordinates: [[...verts, verts[0]]] })
    setVerts([])
  }

  const upload = async (f: File) => {
    try {
      const g = parseBoundary(await f.text())
      setPolygon(g)
      setMsg(null)
      setSource(f.name)
    } catch (e) {
      setMsg((e as Error).message)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      <div ref={el} style={{ height: 420, borderRadius: 'var(--radius-m)', border: '1px solid var(--rule)' }} aria-label="Map" />
      <div className="btn-row">
        {where === 'point' ? (
          <button className="btn btn-primary" disabled={!point || busy} onClick={detect}><Icon name="target" />{busy ? 'Looking up fields' : 'Find fields here'}</button>
        ) : (
          <>
            <button className="btn btn-primary" disabled={verts.length < 3} onClick={finish}><Icon name="check" />Finish boundary</button>
            <button className="btn" disabled={!verts.length && !polygon} onClick={() => { setVerts([]); setPolygon(null) }}>Clear</button>
            <label className="btn"><Icon name="upload" />Upload GeoJSON or KML
              <input type="file" accept=".geojson,.json,.kml" className="sr-only" data-testid="boundary-file"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) upload(f) }} />
            </label>
          </>
        )}
      </div>
      {msg ? <Notice>{msg}</Notice> : null}
      {polygon ? <Notice kind="ok">Field boundary set{source ? ` from ${source}` : ''}.</Notice> : null}
    </div>
  )
}

export function parseBoundary(text: string): GeoJSON {
  const s = text.trim()
  if (s.startsWith('<')) {
    const doc = new DOMParser().parseFromString(s, 'application/xml')
    const c = doc.querySelector('Polygon coordinates, coordinates')
    if (!c?.textContent) throw new Error('No polygon coordinates in this KML file')
    const ring = c.textContent.trim().split(/\s+/).map((t) => t.split(',').slice(0, 2).map(Number) as [number, number])
    if (ring.length < 4) throw new Error('The KML polygon has fewer than 3 corners')
    return { type: 'Polygon', coordinates: [ring] }
  }
  const j = JSON.parse(s)
  const g = j.type === 'FeatureCollection' ? j.features?.[0]?.geometry : j.type === 'Feature' ? j.geometry : j
  if (!g || (g.type !== 'Polygon' && g.type !== 'MultiPolygon')) throw new Error('The file has no Polygon geometry')
  return g.type === 'MultiPolygon' ? { type: 'Polygon', coordinates: g.coordinates[0] } : g
}
