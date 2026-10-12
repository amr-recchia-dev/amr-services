/* AMR Timbra – service worker
 * Tiene in memoria la pagina /timbra così si apre anche senza rete.
 * Le chiamate /api/* NON vengono mai messe in cache: le timbrature
 * offline sono gestite dalla coda nella pagina (localStorage).
 */
const CACHE = 'amr-timbra-v2';
const ASSETS = ['/timbra', '/static/timbra-icon-192.png', '/static/timbra-icon-512.png', '/static/timbra-manifest.json',
  '/static/leaflet/leaflet.js', '/static/leaflet/leaflet.css'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (!ASSETS.includes(url.pathname)) return;

  // Rete prima (versione sempre aggiornata), cache se la rete non c'è
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(url.pathname, copy));
        return resp;
      })
      .catch(() => caches.match(url.pathname))
  );
});
