// The farmer's screen: one decision for today, in the farmer's language, on a phone.
import { useCallback, useEffect, useState } from 'react'
import { api, type Lang, type Outlook, type Today as TodayCard } from '../api'
import { LANG_NAMES } from '../i18n'
import { go, href } from '../router'
import { useApp } from '../state'
import { Icon } from '../ui/icons'
import { Notice } from '../ui/core'

const ICON: Record<string, string> = { spray: 'droplet', wait: 'clock', scan: 'satellite', none: 'leaf' }
const REFRESH_MS = 60_000

export default function Today({ fid }: { fid?: string }) {
  const { farms, lang, setLang, setMode, t, online } = useApp()
  const [card, setCard] = useState<TodayCard | null>(null)
  const [outlook, setOutlook] = useState<Outlook | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updated, setUpdated] = useState<Date | null>(null)
  const ready = farms.filter((f) => f.status === 'ready')
  const id = fid ?? (ready.length === 1 ? ready[0].id : undefined)

  const load = useCallback(async () => {
    if (!id) return
    try {
      const [c, o] = await Promise.all([
        api.get<TodayCard>(`/api/farms/${id}/today`),
        api.get<Outlook>(`/api/farms/${id}/live/outlook`).catch(() => null),
      ])
      setCard(c)
      setOutlook(o)
      setError(null)
      setUpdated(new Date())
    } catch (e) {
      setError((e as Error).message)
    }
  }, [id])
  useEffect(() => {
    setCard(null)
    load()
    const h = window.setInterval(load, REFRESH_MS)
    return () => window.clearInterval(h)
  }, [load])

  const date = (s: string) => new Date(s.length === 10 ? s + 'T12:00:00' : s).toLocaleDateString(lang, { weekday: 'long', day: 'numeric', month: 'long' })
  const dayShort = (s: string) => new Date(s + 'T12:00:00').toLocaleDateString(lang, { weekday: 'short', day: 'numeric' })
  const params = (p: Record<string, unknown>) => ({
    ...p,
    p: typeof p.p === 'number' ? Math.round(p.p * 100) : p.p,
    wind: typeof p.wind === 'number' ? p.wind.toFixed(1) : p.wind,
    rain: typeof p.rain === 'number' ? p.rain.toFixed(1) : p.rain,
    due: typeof p.due === 'string' ? date(p.due) : p.due,
    last: typeof p.last === 'string' ? date(p.last) : p.last,
  })

  const toTeam = () => {
    setMode('team')
    go(id ? href.farm(id, 'mission') : href.farms)
  }

  return (
    <div className="farmer">
      <header className="farmer-top">
        <a className="brand" href={href.today(id)}><img src="/icon.svg" alt="" />Future Agri</a>
        <span className="spacer" />
        <label className="small muted" htmlFor="farmer-lang">{t('today.language')}</label>
        <select id="farmer-lang" className="input" style={{ width: 'auto', minHeight: 40 }} value={lang} onChange={(e) => setLang(e.target.value as Lang)}>
          {(Object.keys(LANG_NAMES) as Lang[]).map((l) => <option key={l} value={l}>{LANG_NAMES[l]}</option>)}
        </select>
        <button type="button" className="btn" onClick={toTeam}>{t('mode.team')}</button>
      </header>

      <main className="farmer-main">
        {!online ? <Notice kind="warn">{t('offline')}</Notice> : null}
        {!id ? (
          <>
            <h1>{t('today.pick_farm')}</h1>
            {ready.length ? (
              <nav className="farm-pick">
                {ready.map((f) => <a key={f.id} href={href.today(f.id)}>{f.name}<Icon name="chevron" /></a>)}
              </nav>
            ) : <p className="muted">{t('today.no_farms')}</p>}
          </>
        ) : error && !card ? (
          <Notice kind="bad">
            {t('today.error')}: {error}{' '}
            <button className="btn btn-s" onClick={load}>{t('act.retry')}</button>
          </Notice>
        ) : !card ? (
          <p className="muted" aria-busy="true">…</p>
        ) : (
          <>
            <div>
              <h1>{t('today.title', { farm: card.farm.name })}</h1>
              <p className="muted">{t('today.day', { date: date(card.day), das: card.das })}</p>
            </div>

            <section className={`advice ${card.status}`} data-testid="advice" aria-live="polite">
              <span className="advice-icon"><Icon name={ICON[card.status] ?? 'leaf'} size={30} /></span>
              <div>
                <h2>{t(`today.${card.code}`, params(card.params))}</h2>
                <p>{t(`today.${card.code}.detail`, params(card.params))}</p>
                {card.next && card.code !== 'spray_today' ? (
                  <p style={{ fontWeight: 600, color: 'var(--ink)' }}>{t('today.next', { product: card.next.product, due: date(card.next.due) })}</p>
                ) : null}
              </div>
            </section>

            <section aria-labelledby="out-h">
              <h3 id="out-h" style={{ marginBottom: 8 }}>{t('today.outlook')}</h3>
              {outlook?.available && outlook.days.length ? (
                <div className="days">
                  {outlook.days.slice(0, 7).map((d) => {
                    const pct = Math.round(d.p_flyable * 100)
                    return (
                      <div key={d.day} className={`day${pct < 25 ? ' bad' : pct < 50 ? ' poor' : ''}`} title={t('today.flyable', { p: pct })}>
                        <b>{dayShort(d.day)}</b>
                        <div className="meter" aria-hidden="true"><i style={{ width: `${pct}%` }} /></div>
                        <span>{pct}%</span>
                        <small>{t('today.wind', { wind: d.wind_dawn_p50.toFixed(1) })}</small>
                        <small>{t('today.rain', { rain: d.rain_p50.toFixed(1) })}</small>
                      </div>
                    )
                  })}
                </div>
              ) : <p className="muted small">{t('today.no_outlook')}</p>}
            </section>

            <section aria-labelledby="health-h">
              <h3 id="health-h" style={{ marginBottom: 8 }}>{t('today.health')}</h3>
              <div className="stats">
                <div className="stat"><b>{card.health.stage_code ? t(`stage.${card.health.stage_code}`) : '–'}</b><span>{t('today.stage')}</span></div>
                <div className="stat"><b>{card.health.lai === null ? '–' : card.health.lai.toFixed(1)}</b><span>{t('today.leaf_area')}</span></div>
              </div>
            </section>

            <section aria-labelledby="alerts-h">
              <h3 id="alerts-h" style={{ marginBottom: 8 }}>{t('today.alerts')}</h3>
              {card.alerts.length ? (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {card.alerts.map((a, i) => {
                    const key = `alert.${a.code}`
                    const text = t(key, params(a.params))
                    return <Notice key={i} kind={a.level === 'alert' ? 'bad' : a.level === 'warn' ? 'warn' : ''}>{text === key ? a.text : text}</Notice>
                  })}
                </div>
              ) : <p className="muted small">{t('today.no_alerts')}</p>}
            </section>
            {updated ? <p className="xs faint">{t('today.updated', { time: updated.toLocaleTimeString(lang, { hour: '2-digit', minute: '2-digit' }) })}</p> : null}
          </>
        )}
      </main>
    </div>
  )
}
