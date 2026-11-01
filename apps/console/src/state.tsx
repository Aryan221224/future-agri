import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, type Farm, type Lang, type Meta, type SessionState, type Settings } from './api'
import { translate } from './i18n'
import { load, save } from './store'

type Toast = { id: number; text: string; kind: '' | 'bad' }

interface AppValue {
  meta: Meta | null
  settings: Settings
  saveSettings: (s: Partial<Settings>) => Promise<void>
  farms: Farm[]
  reloadFarms: () => Promise<void>
  lang: Lang
  setLang: (l: Lang) => void
  mode: 'team' | 'farmer'
  setMode: (m: 'team' | 'farmer') => void
  t: (key: string, params?: Record<string, unknown>) => string
  toast: (text: string, kind?: '' | 'bad') => void
  toasts: Toast[]
  dismiss: (id: number) => void
  online: boolean
}

const DEFAULT_SETTINGS: Settings = { area_unit: 'ha', language: 'en', theme: 'dark', operator: '', default_region: '', aircraft_id: 'K3A-01' }
const AppCtx = createContext<AppValue | null>(null)

export function useApp(): AppValue {
  const v = useContext(AppCtx)
  if (!v) throw new Error('useApp outside AppProvider')
  return v
}

export function AppProvider({ children }: { children: ReactNode }) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS)
  const [farms, setFarms] = useState<Farm[]>([])
  const [lang, setLangState] = useState<Lang>(() => load<Lang>('lang', 'en'))
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
    api.get<Settings>('/api/settings').then(setSettings).catch(() => undefined)
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

  useEffect(() => {
    const root = document.documentElement
    if (settings.theme === 'auto') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', settings.theme)
  }, [settings.theme])

  useEffect(() => { document.documentElement.lang = lang }, [lang])

  const saveSettings = useCallback(async (s: Partial<Settings>) => {
    const next = await api.post<Settings>('/api/settings', s)
    setSettings(next)
  }, [])

  const value = useMemo<AppValue>(() => ({
    meta, settings, saveSettings, farms, reloadFarms, lang,
    setLang: (l) => { setLangState(l); save('lang', l) },
    mode, setMode: (m) => { setModeState(m); save('mode', m) },
    t: (key, params) => translate(lang, key, params), toast, toasts, dismiss, online,
  }), [meta, settings, saveSettings, farms, reloadFarms, lang, mode, toast, toasts, dismiss, online])

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
