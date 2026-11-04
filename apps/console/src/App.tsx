import { lazy, Suspense, type ReactNode } from 'react'
import { LOCALE } from './i18n'
import { FarmProvider, useApp, useFarm, AppProvider } from './state'
import { go, href, useRoute, type Page, type Route } from './router'
import { Icon } from './ui/icons'
import { ThemeToggle } from './ui/ThemeToggle'
import { Empty, Progress } from './ui/core'
import { Farms } from './pages/Farms'

const NewFarm = lazy(() => import('./pages/NewFarm'))
const Mission = lazy(() => import('./pages/Mission'))
const Twin3D = lazy(() => import('./pages/Twin3D'))
const Drone = lazy(() => import('./pages/Drone'))
const Season = lazy(() => import('./pages/Season'))
const Iterate = lazy(() => import('./pages/Iterate'))
const PlanPage = lazy(() => import('./pages/Plan'))
const Live = lazy(() => import('./pages/Live'))
const Records = lazy(() => import('./pages/Records'))
const LiveRun = lazy(() => import('./pages/LiveRun'))
const Aircraft = lazy(() => import('./pages/Aircraft'))
const System = lazy(() => import('./pages/System'))
const Today = lazy(() => import('./pages/Today'))

const NAV: { page: Page; icon: string; key: string }[] = [
  { page: 'mission', icon: 'loop', key: 'nav.mission' },
  { page: 'twin', icon: 'cube', key: 'nav.twin' },
  { page: 'drone', icon: 'drone', key: 'nav.drone' },
  { page: 'season', icon: 'chart', key: 'nav.season' },
  { page: 'iterate', icon: 'flask', key: 'nav.iterate' },
  { page: 'plan', icon: 'clipboard', key: 'nav.plan' },
  { page: 'live', icon: 'signal', key: 'nav.live' },
  { page: 'aircraft', icon: 'drone', key: 'nav.aircraft' },
  { page: 'records', icon: 'book', key: 'nav.records' },
]

export function App() {
  return (
    <AppProvider>
      <Root />
    </AppProvider>
  )
}

function Root() {
  const route = useRoute()
  const { mode } = useApp()
  if (mode === 'farmer' || route.name === 'today') {
    return (
      <Suspense fallback={<Loading />}>
        <Today fid={route.name === 'today' ? route.fid : route.name === 'farm' ? route.fid : undefined} />
        <Toasts />
      </Suspense>
    )
  }
  const page = route.name === 'farm'
    ? <FarmProvider fid={route.fid}><Shell route={route}><FarmPage route={route} /></Shell></FarmProvider>
    : <Shell route={route}><GlobalPage route={route} /></Shell>
  return <>{page}<Toasts /></>
}

function GlobalPage({ route }: { route: Route }) {
  return (
    <Suspense fallback={<Loading />}>
      {route.name === 'newfarm' ? <NewFarm /> : route.name === 'system' ? <System /> : <Farms />}
    </Suspense>
  )
}

function FarmPage({ route }: { route: Extract<Route, { name: 'farm' }> }) {
  const { farm } = useFarm()
  const { farms } = useApp()
  if (!farm) {
    return farms.length === 0 ? <Loading /> : (
      <div className="page"><Empty title="This farm does not exist" action={<a className="btn" href={href.farms}>Back to farms</a>}>It may have been deleted.</Empty></div>
    )
  }
  if (farm.status !== 'ready') return <div className="page"><BuildState /></div>
  const pages: Record<Page, ReactNode> = {
    mission: <Mission />, twin: <Twin3D />, drone: <Drone />, season: <Season />, iterate: <Iterate />,
    plan: <PlanPage />, live: <Live />, records: <Records />, 'live-run': <LiveRun />, aircraft: <Aircraft />,
  }
  return <Suspense fallback={<Loading />}>{pages[route.page]}</Suspense>
}

function BuildState() {
  const { farm } = useFarm()
  if (!farm) return null
  if (farm.status === 'error') {
    return (
      <Empty title="This farm could not be built" action={<a className="btn" href={href.farms}>Back to farms</a>}>
        {farm.meta.error}
      </Empty>
    )
  }
  return (
    <div style={{ maxWidth: 520 }}>
      <h2>Building {farm.name}</h2>
      <p className="muted" style={{ margin: '6px 0 14px' }}>Fetching real data for the field: boundary, soil, terrain, weather and satellite.</p>
      <Progress indeterminate />
    </div>
  )
}

function Shell({ route, children }: { route: Route; children: ReactNode }) {
  const { t, meta, farms, online, resolvedTheme } = useApp()
  const fid = route.name === 'farm' ? route.fid : null
  const wide = route.name === 'farm' && (route.page === 'twin' || route.page === 'drone')
  return (
    <div className="shell">
      <header className="clock">
        <a className="brand" href={href.farms}><img src="/icon.svg" alt="" /><span>Future Agri</span></a>
        <div className="clock-farm">{fid ? <FarmSwitch fid={fid} page={route.name === 'farm' ? route.page : 'mission'} /> : null}{fid ? <MissionClock /> : null}</div>
        <div className="clock-tools"><ThemeToggle /><ModeSwitch /></div>
      </header>
      {meta?.lan_exposed ? <div className="banner">No login: anyone who can reach this computer on the network can operate this console.</div> : null}
      {!online ? <div className="banner">{t('offline')}</div> : null}
      <nav className="rail" aria-label="Main">
        <a href={href.farms} aria-current={route.name === 'farms' || route.name === 'newfarm' ? 'page' : undefined}><Icon name="fields" />{t('nav.farms')}</a>
        {fid ? <>
          <div className="rail-sep" />
          <div className="rail-head">{farms.find((f) => f.id === fid)?.name}</div>
          {NAV.map((n) => (
            <a key={n.page} href={href.farm(fid, n.page)} aria-current={route.name === 'farm' && route.page === n.page ? 'page' : undefined}>
              <Icon name={n.icon} />{t(n.key)}
            </a>
          ))}
        </> : null}
        <div className="rail-sep" />
        <a href={href.today(fid ?? undefined)}><Icon name="phone" />{t('nav.today')}</a>
        <a href={href.system} aria-current={route.name === 'system' ? 'page' : undefined}><Icon name="gear" />{t('nav.system')}</a>
      </nav>
      <main className="main" id="main">
        {/* remount on a theme change so 3D views and charts redraw in the new colours */}
        <div className={wide ? 'page-wide' : 'page'} key={resolvedTheme}>{children}</div>
      </main>
      <nav className="bottom-nav" aria-label="Main">
        {fid ? NAV.filter((n) => ['mission', 'twin', 'drone', 'live'].includes(n.page)).map((n) => (
          <a key={n.page} href={href.farm(fid, n.page)} aria-current={route.name === 'farm' && route.page === n.page ? 'page' : undefined}><Icon name={n.icon} />{t(n.key)}</a>
        )) : null}
        <a href={href.farms} aria-current={route.name === 'farms' ? 'page' : undefined}><Icon name="fields" />{t('nav.farms')}</a>
        <a href={href.system}><Icon name="gear" />{t('nav.more')}</a>
      </nav>
    </div>
  )
}

function FarmSwitch({ fid, page }: { fid: string; page: Page }) {
  const { farms } = useApp()
  return (
    <select aria-label="Open farm" value={fid} onChange={(e) => go(href.farm(e.target.value, page))}>
      {farms.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
    </select>
  )
}

function MissionClock() {
  const { session } = useFarm()
  const { t } = useApp()
  if (!session) return null
  const total = Math.round((new Date(session.harvest).getTime() - new Date(session.sowing).getTime()) / 86400000)
  const phase = session.done ? t('clock.done') : session.world.waiting ? t('clock.waiting') : session.phase === 'flights' ? t('clock.flights') : t('clock.morning')
  return (
    <span className="clock-day num">
      {new Date(session.day + 'T12:00:00').toLocaleDateString(LOCALE, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })}
      {' — '}{t('clock.day', { das: Math.min(session.das, total), total })}{', '}{phase}
    </span>
  )
}

function ModeSwitch() {
  const { mode, setMode, t } = useApp()
  return (
    <div className="seg" role="group" aria-label="View">
      <button type="button" aria-pressed={mode === 'team'} onClick={() => setMode('team')}>{t('mode.team')}</button>
      <button type="button" aria-pressed={mode === 'farmer'} onClick={() => setMode('farmer')}>{t('mode.farmer')}</button>
    </div>
  )
}

export function Toasts() {
  const { toasts, dismiss } = useApp()
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((x) => (
        <div key={x.id} className={`toast ${x.kind}`}>
          <span style={{ flex: 1 }}>{x.text}</span>
          <button className="btn btn-quiet btn-s" style={{ color: 'inherit', minHeight: 24, padding: 2 }} aria-label="Dismiss" onClick={() => dismiss(x.id)}><Icon name="x" size={16} /></button>
        </div>
      ))}
    </div>
  )
}

function Loading() {
  return <div className="page" style={{ maxWidth: 420 }}><Progress indeterminate /></div>
}
