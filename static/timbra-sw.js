/* AMR Timbra – service worker
 * Tiene in memoria la pagina /timbra così si apre anche senza rete o con rete bloccata.
 * Le chiamate /api/* NON vengono mai messe in cache: le timbrature
 * offline sono gestite dalla coda nella pagina (localStorage).
 */
const CACHE = 'amr-timbra-v3';
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

function fetchWithTimeout(req, ms) {
  if (typeof AbortController === 'undefined') return fetch(req);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  return fetch(req, { signal: ctrl.signal }).finally(() => clearTimeout(timer));
}

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/')) return;
  if (!ASSETS.includes(url.pathname)) return;

  // Rete con timeout rapido (2.5s) per evitare schermo bianco su iPhone quando il 4G/Wi-Fi in capannone è bloccato.
  // Salva in cache SOLO risposte HTTP 200 valide.
  e.respondWith(
    fetchWithTimeout(e.request, 2500)
      .then((resp) => {
        if (!resp || !resp.ok || resp.status !== 200) {
          return caches.match(url.pathname).then((cached) => cached || resp);
        }
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(url.pathname, copy));
        return resp;
      })
      .catch(() => caches.match(url.pathname))
  );
});
