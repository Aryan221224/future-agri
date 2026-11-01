// Offline support: the app shell is cached; API reads are network-first with the
// last good response as the fallback, so a phone in the field still shows the
// latest advice it saw. Live streams (SSE) and writes are never cached.
const SHELL = 'fa-shell-v1'
const DATA = 'fa-data-v1'

self.addEventListener('install', (e) => {
  self.skipWaiting()
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(['/', '/icon.svg', '/manifest.webmanifest'])))
})

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL && k !== DATA).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

const offline = () => new Response(JSON.stringify({ detail: 'offline and not saved on this device yet' }),
  { status: 503, headers: { 'Content-Type': 'application/json' } })

self.addEventListener('fetch', (e) => {
  const req = e.request
  const url = new URL(req.url)
  if (req.method !== 'GET' || url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/')) {
    if (url.pathname.endsWith('/events')) return
    e.respondWith(
      fetch(req).then((r) => {
        if (r.ok) { const copy = r.clone(); caches.open(DATA).then((c) => c.put(req, copy)) }
        return r
      }).catch(() => caches.match(req).then((m) => m || offline())),
    )
    return
  }
  if (req.mode === 'navigate') {
    e.respondWith(fetch(req).catch(() => caches.match('/')))
    return
  }
  e.respondWith(
    caches.match(req).then((m) => m || fetch(req).then((r) => {
      if (r.ok && url.pathname.startsWith('/assets/')) { const copy = r.clone(); caches.open(SHELL).then((c) => c.put(req, copy)) }
      return r
    })),
  )
})
