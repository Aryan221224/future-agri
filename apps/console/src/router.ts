import { useEffect, useState } from 'react'

export type Page = 'mission' | 'twin' | 'drone' | 'season' | 'iterate' | 'plan' | 'live' | 'records' | 'live-run'
export const PAGES: Page[] = ['mission', 'twin', 'drone', 'season', 'iterate', 'plan', 'live', 'records', 'live-run']
export type Route =
  | { name: 'farms' }
  | { name: 'newfarm' }
  | { name: 'system' }
  | { name: 'today'; fid?: string }
  | { name: 'farm'; fid: string; page: Page; arg?: string }

export function parse(hash: string): Route {
  const p = hash.replace(/^#\/?/, '').split('?')[0].split('/').filter(Boolean)
  if (p[0] === 'farms' && p[1] === 'new') return { name: 'newfarm' }
  if (p[0] === 'system') return { name: 'system' }
  if (p[0] === 'today') return { name: 'today', fid: p[1] }
  if (p[0] === 'f' && p[1]) {
    const page = (PAGES as string[]).includes(p[2]) ? (p[2] as Page) : 'mission'
    return { name: 'farm', fid: p[1], page, arg: p[3] }
  }
  return { name: 'farms' }
}

export function useRoute(): Route {
  const [r, setR] = useState(() => parse(location.hash))
  useEffect(() => {
    const on = () => setR(parse(location.hash))
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return r
}

export const href = {
  farms: '#/farms',
  newFarm: '#/farms/new',
  system: '#/system',
  today: (fid?: string) => (fid ? `#/today/${fid}` : '#/today'),
  farm: (fid: string, page: Page = 'mission', arg?: string) => `#/f/${fid}/${page}${arg ? '/' + arg : ''}`,
}

export function go(h: string) {
  location.hash = h
}
