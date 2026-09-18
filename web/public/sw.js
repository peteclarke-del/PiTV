// PiTV's service worker: makes the site installable on a phone and keeps the shell openable
// when the Pi is briefly unreachable. Hashed assets are cached as they are fetched (their names
// change with every build, so an old cache can never serve a stale file). The API, the streams
// and index.html always go to the network: what is on changes by the minute, and index.html
// names the current build.
const SHELL = 'pitv-shell-v1';
const NEVER_CACHE = /^\/(api\/|channel\/|assets\/hls)/;

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== location.origin || NEVER_CACHE.test(url.pathname)) return;
  if (url.pathname.startsWith('/assets/') || /\.(png|json)$/.test(url.pathname)) {
    event.respondWith(caches.open(SHELL).then(async (cache) => {
      const hit = await cache.match(event.request);
      if (hit) return hit;
      const res = await fetch(event.request);
      if (res.ok) cache.put(event.request, res.clone());
      return res;
    }));
    return;
  }
  // index.html and everything else: the network, with the last good copy when it is down.
  event.respondWith(fetch(event.request).then((res) => {
    if (res.ok) caches.open(SHELL).then((cache) => cache.put(event.request, res.clone()));
    return res;
  }).catch(() => caches.match(event.request).then((hit) => hit || caches.match('/'))));
});
