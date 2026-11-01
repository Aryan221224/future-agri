import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, type Farm, type Meta, type SessionState, type Settings } from './api'
import { translate } from './i18n'
import { load, save } from './store'

type Toast = { id: number; text: string; kind: '' | 'bad' }

interface AppValue {
  meta: Meta | null
  settings: Settings
  saveSettings: (s: Partial<Settings>) => Promise<void>
  farms: Farm[]
  reloadFarms: () => Promise<void>
  mode: 'team' | 'farmer'
  setMode: (m: 'team' | 'farmer') => void
  /** 'dark' or 'light' as shown now ('auto' follows the device). */
  resolvedTheme: 'dark' | 'light'
  setTheme: (t: 'dark' | 'light' | 'auto') => void
  t: (key: string, params?: Record<string, unknown>) => string
  toast: (text: string, kind?: '' | 'bad') => void
  toasts: Toast[]
  dismiss: (id: number) => void
  online: boolean
}

const DEFAULT_SETTINGS: Settings = { area_unit: 'ha', language: 'en', theme: 'dark', operator: '', default_region: '', aircraft_id: 'K3A-01' }
const THEME_COLOR = { dark: '#18140f', light: '#edf1ee' }

function systemDark(): boolean {
  return typeof window !== 'undefined' && (window.matchMedia?.('(prefers-color-scheme: dark)').matches ?? false)
}

function resolve(theme: Settings['theme'], sysDark: boolean): 'dark' | 'light' {
  return theme === 'auto' ? (sysDark ? 'dark' : 'light') : theme
}

/** Applied to the page at once, so views remounted for the new theme read the right colours. */
function applyTheme(theme: Settings['theme'], sysDark: boolean): void {
  const root = document.documentElement
  if (theme === 'auto') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLOR[resolve(theme, sysDark)])
}
const AppCtx = createContext<AppValue | null>(null)

export function useApp(): AppValue {
  const v = useContext(AppCtx)
  if (!v) throw new Error('useApp outside AppProvider')
  return v
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [settings, setSettings] = useState<Settings>(() => ({ ...DEFAULT_SETTINGS, theme: load<Settings['theme']>('theme', 'dark') }))
  const [sysDark, setSysDark] = useState(systemDark)
  const [farms, setFarms] = useState<Farm[]>([])
  const [mode, setModeState] = useState<'team' | 'farmer'>(() => load('mode', 'team'))
  const [toasts, setToasts] = useState<Toast[]>([])
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine)
  const seq = useRef(0)

  const toast = useCallback((text: string, kind: '' | 'bad' = '') => {
    const id = ++seq.current
    setToasts((ts) => [...ts.slice(-3), { id, text, kind }])
    window.setTimeout(() => setToasts((ts) => ts.filter((x) => x.id !== id)), kind === 'bad' ? 9000 : 4500)
  }, [])
  const dismiss = useCallback((id: number) => setToasts((ts) => ts.filter((x) => x.id !== id)), [])

  const reloadFarms = useCallback(async () => {
    try {
      const r = await api.get<{ farms: Farm[] }>('/api/farms')
      setFarms(r.farms)
    } catch (e) {
      toast(`Could not load farms: ${(e as Error).message}`, 'bad')
    }
  }, [toast])

  useEffect(() => {
    api.get<Meta>('/api/meta').then(setMeta).catch((e) => toast(`Could not reach the console server: ${e.message}`, 'bad'))
    api.get<Settings>('/api/settings').then((s) => { applyTheme(s.theme, systemDark()); save('theme', s.theme); setSettings(s) }).catch(() => undefined)
    reloadFarms()
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) }
  }, [reloadFarms, toast])

  // keep the farm list fresh while a farm is being built
  useEffect(() => {
    if (!farms.some((f) => f.status === 'queued' || f.status === 'building')) return
    const h = window.setInterval(reloadFarms, 3000)
    return () => window.clearInterval(h)
  }, [farms, reloadFarms])

  useEffect(() => { applyTheme(settings.theme, sysDark) }, [settings.theme, sysDark])
  useEffect(() => {
    const mq = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!mq) return
    const on = () => setSysDark(mq.matches)
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  // optimistic: the page changes at once; the server copy follows (and is undone if it fails)
  const saveSettings = useCallback(async (s: Partial<Settings>) => {
    let prev: Settings | null = null
    setSettings((cur) => { prev = cur; return { ...cur, ...s } })
    try {
      setSettings(await api.post<Settings>('/api/settings', s))
    } catch (e) {
      if (prev) setSettings(prev)
      throw e
    }
  }, [])

  const setTheme = useCallback((theme: Settings['theme']) => {
    applyTheme(theme, systemDark())
    save('theme', theme)
    saveSettings({ theme }).catch((e) => toast(`Theme not saved on the console: ${(e as Error).message}`, 'bad'))
  }, [saveSettings, toast])
  const resolvedTheme = resolve(settings.theme, sysDark)

  const value = useMemo<AppValue>(() => ({
    meta, settings, saveSettings, farms, reloadFarms, resolvedTheme, setTheme,
    mode, setMode: (m) => { setModeState(m); save('mode', m) },
    t: (key, params) => translate(key, params), toast, toasts, dismiss, online,
  }), [meta, settings, saveSettings, farms, reloadFarms, resolvedTheme, setTheme, mode, toast, toasts, dismiss, online])

  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>
}

// ------------------------------------------------------------------ the open farm and its mission session
interface FarmValue {
  farm: Farm | null
  session: SessionState | null
  sessionError: string | null
  refresh: () => Promise<void>
  setSession: (s: SessionState) => void
}
const FarmCtx = createContext<FarmValue>({ farm: null, session: null, sessionError: null, refresh: async () => undefined, setSession: () => undefined })

export function useFarm(): FarmValue {
  return useContext(FarmCtx)
}

export function FarmProvider({ fid, children }: { fid: string; children: ReactNode }) {
  const { farms } = useApp()
  const farm = farms.find((f) => f.id === fid) ?? null
  const [session, setSession] = useState<SessionState | null>(null)
  const [sessionError, setError] = useState<string | null>(null)
  const ready = farm?.status === 'ready'
  const refresh = useCallback(async () => {
    if (!ready) return
    try {
      setSession(await api.get<SessionState>(`/api/farms/${fid}/session`))
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [fid, ready])
  useEffect(() => { setSession(null); refresh() }, [refresh])
  const value = useMemo(() => ({ farm, session, sessionError, refresh, setSession }), [farm, session, sessionError, refresh])
  return <FarmCtx.Provider value={value}>{children}</FarmCtx.Provider>
}
