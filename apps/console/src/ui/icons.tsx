const P: Record<string, string> = {
  fields: 'M3 17l9 4 9-4M3 12l9 4 9-4M3 7l9 4 9-4-9-4z',
  loop: 'M17 2l4 4-4 4M3 11V9a3 3 0 013-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 01-3 3H3',
  cube: 'M21 16V8l-9-5-9 5v8l9 5 9-5zM3.3 7L12 12l8.7-5M12 22V12',
  drone: 'M4 4h4M16 4h4M4 20h4M16 20h4M6 4v2l4 4M18 4v2l-4 4M6 20v-2l4-4M18 20v-2l-4-4M10 10h4v4h-4z',
  chart: 'M3 3v18h18M7 15l4-4 3 3 6-6',
  flask: 'M9 3h6M10 3v6L4 19a1 1 0 001 2h14a1 1 0 001-2l-6-10V3M7 14h10',
  clipboard: 'M9 3h6v3H9zM8 4.5H6a1 1 0 00-1 1V20a1 1 0 001 1h12a1 1 0 001-1V5.5a1 1 0 00-1-1h-2M9 12h6M9 16h4',
  signal: 'M2 12h3l3-8 4 16 3-8h7',
  book: 'M4 19V5a2 2 0 012-2h13v16H6a2 2 0 00-2 2zm0 0a2 2 0 002 2h13',
  gear: 'M12 15a3 3 0 100-6 3 3 0 000 6zM12 2v3M12 19v3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M2 12h3M19 12h3M4.9 19.1L7 17M17 7l2.1-2.1',
  plus: 'M12 5v14M5 12h14',
  play: 'M7 4l13 8-13 8z',
  pause: 'M8 4v16M16 4v16',
  skip: 'M5 4l10 8-10 8zM19 5v14',
  forward: 'M3 4l8 8-8 8zM12 4l8 8-8 8z',
  check: 'M5 12l5 5L20 7',
  x: 'M6 6l12 12M18 6L6 18',
  alert: 'M12 9v4M12 17h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  download: 'M12 4v12M7 11l5 5 5-5M4 20h16',
  map: 'M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2zM9 4v14M15 6v14',
  moon: 'M20.5 14.5A8.5 8.5 0 019.5 3.5a8.5 8.5 0 1011 11z',
  sun: 'M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4',
  leaf: 'M11 20A7 7 0 014 13c0-6 7-10 16-10 0 9-4 16-10 16zM4 21c3-5 6-8 10-10',
  satellite: 'M13 7L9 3 5 7l4 4M17 11l4 4-4 4-4-4M8 12l4 4M16 8l-4 4M3 21l3-3',
  refresh: 'M21 12a9 9 0 11-3-6.7L21 8M21 3v5h-5',
  chevron: 'M9 6l6 6-6 6',
  back: 'M15 6l-6 6 6 6',
  home: 'M3 11l9-8 9 8M5 10v10h14V10',
  target: 'M12 22a10 10 0 100-20 10 10 0 000 20zM12 18a6 6 0 100-12 6 6 0 000 12zM12 14a2 2 0 100-4 2 2 0 000 4z',
  droplet: 'M12 2.7l5.7 5.6a8 8 0 11-11.3 0z',
  wind: 'M3 8h10a3 3 0 10-3-3M3 12h15a3 3 0 11-3 3M3 16h7',
  battery: 'M3 8h15v8H3zM21 11v2M6 11v2M9 11v2',
  clock: 'M12 22a10 10 0 100-20 10 10 0 000 20zM12 6v6l4 2',
  phone: 'M7 2h10v20H7zM11 18h2',
  trash: 'M4 7h16M10 11v6M14 11v6M5 7l1 13h12l1-13M9 7V4h6v3',
}

export function Icon({ name, size = 20, title }: { name: string; size?: number; title?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? 'img' : undefined}>
      {title ? <title>{title}</title> : null}
      <path d={P[name] ?? P.alert} />
    </svg>
  )
}
